"""WS ``/v1/voice/live`` client for the always-on ears process.

The ears loop owns the microphone 24/7. After the wake door opens a voice
session, the conversation moves to the full-duplex WebSocket so raw PCM16
flows continuously, and partials, backchannels, barge-in signals, and playable
TTS chunks stream back on the same connection.

This module deliberately knows nothing about VAD, wake words, or chat. It is a
small transport + cancellable playback pair; ``main.py`` decides when to open
it and what each event means.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger("ears.live")


class EarsLiveUnavailable(RuntimeError):
    """The live door refused this session (disabled, not-live, or network)."""

    def __init__(self, code: str = "live_unavailable", message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def live_ws_url(api_url: str, session_id: str) -> str:
    """Build ``ws(s)://host/v1/voice/live?session_id=…`` from the API URL."""

    parsed = urlparse(api_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base = parsed.netloc or parsed.path
    return f"{scheme}://{base}/v1/voice/live?session_id={session_id}"


async def _connect_websocket(url: str, *, api_key: str | None) -> Any:
    from websockets.asyncio.client import connect

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # Server JSON frames can carry up to ~2 MB of base64 TTS audio. The
    # default 1 MB receive limit would drop those chunks.
    return await connect(
        url,
        additional_headers=headers,
        open_timeout=5,
        close_timeout=2,
        max_size=8 * 1024 * 1024,
    )


class EarsLiveChannel:
    """One persistent full-duplex conversation socket."""

    def __init__(self, ws: Any, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._ws = ws
        self._loop = loop or asyncio.get_event_loop()
        # Bounded queue keeps the microphone loop non-blocking: a slow socket
        # drops old PCM rather than growing memory or stalling capture.
        self._send_queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(maxsize=512)
        self._sender = self._loop.create_task(self._send_loop(), name="ears-live-send")
        self._closed = False

    @classmethod
    async def open(
        cls,
        *,
        api_url: str,
        session_id: str,
        api_key: str | None = None,
        connect: Callable[..., Awaitable[Any]] | None = None,
    ) -> EarsLiveChannel:
        """Connect, validate the server ``ready`` event, and return the channel."""

        if not api_url:
            raise EarsLiveUnavailable("no_api_url", "no EV API URL configured")
        connector = connect or _connect_websocket
        try:
            ws = await connector(
                live_ws_url(api_url, session_id),
                api_key=api_key,
            )
        except Exception as exc:  # noqa: BLE001 - fallback is the caller's job
            raise EarsLiveUnavailable(
                "live_connect_failed", f"live socket connect failed: {exc}"
            ) from exc
        channel = cls(ws)
        try:
            first = await channel.receive()
        except Exception as exc:  # noqa: BLE001
            await channel.close()
            raise EarsLiveUnavailable(
                "live_handshake_failed", f"live handshake failed: {exc}"
            ) from exc
        if first.get("type") == "error":
            code = str(first.get("code") or "live_unavailable")
            message = str(first.get("message") or code)
            await channel.close()
            raise EarsLiveUnavailable(code, message)
        if first.get("type") != "ready":
            await channel.close()
            raise EarsLiveUnavailable(
                "live_not_ready", f"unexpected first live event: {first.get('type')}"
            )
        return channel

    @property
    def closed(self) -> bool:
        return self._closed

    async def receive(self) -> dict[str, Any]:
        """Wait for the next JSON event from the server."""

        if self._closed:
            raise EarsLiveUnavailable("live_closed", "live channel is closed")
        message = await self._ws.recv()
        if isinstance(message, (bytes, bytearray)):
            message = bytes(message).decode("utf-8")
        try:
            payload = json.loads(message)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"type": "unknown", "raw": str(message)[:200]}
        if not isinstance(payload, dict):
            return {"type": "unknown", "raw": str(message)[:200]}
        return payload

    def offer_pcm(self, pcm: bytes) -> None:
        """Queue raw PCM16 for sending without ever blocking the mic loop."""

        if self._closed or not pcm:
            return
        self._try_put(("bytes", pcm))

    async def send_pcm(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        await self._send_queue.put(("bytes", pcm))

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        await self._send_queue.put(("text", json.dumps(payload)))

    async def send_text(self, text: str, *, commit: bool = True) -> None:
        await self.send_json({"type": "text", "text": text, "commit": commit})

    async def send_control(self, action: str) -> None:
        await self.send_json({"type": "control", "action": action})

    async def send_audio_segment(self, pcm: bytes, *, chunk_size: int = 32_000) -> None:
        """Replay one already-captured utterance, then mark speech complete.

        Wake clips are captured before the channel exists. Sending their PCM
        in-place and then an explicit ``speech: false`` transition lets the
        server's VAD see a single finished utterance without waiting for more
        microphone blocks.
        """

        for offset in range(0, len(pcm), chunk_size):
            await self.send_pcm(pcm[offset : offset + chunk_size])
        await self.send_json({"type": "speech", "active": False})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(asyncio.QueueFull):
            self._send_queue.put_nowait(None)
        sender = self._sender
        if sender is not None and not sender.done():
            # Flush already-queued frames briefly, then cut the socket. A
            # barge-in close must never hang on a wedged transport.
            try:
                await asyncio.wait_for(asyncio.shield(sender), timeout=2)
            except TimeoutError:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def _send_loop(self) -> None:
        try:
            while True:
                item = await self._send_queue.get()
                if item is None:
                    return
                _kind, payload = item
                try:
                    await self._ws.send(payload)
                except Exception:  # noqa: BLE001 - a dead socket ends the loop
                    self._closed = True
                    return
        finally:
            self._closed = True

    def _try_put(self, item: tuple[str, Any]) -> None:
        try:
            self._send_queue.put_nowait(item)
        except asyncio.QueueFull:
            # Drop the oldest binary PCM frame to keep the newest speech.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._send_queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._send_queue.put_nowait(item)


async def _spawn_afplay(path: str) -> Any:
    return await asyncio.create_subprocess_exec("/usr/bin/afplay", "-q", "1", path)


class EarsLivePlayer:
    """Sequential, cancellable TTS playback for the live loop.

    Playback runs in its own worker so the receive loop keeps draining
    ``barge_in`` events while audio is playing. ``stop()`` terminates the
    current ``afplay`` process and discards queued chunks.
    """

    def __init__(
        self,
        *,
        fetch_audio: Callable[[str], Awaitable[bytes | None]] | None = None,
        spawn: Callable[[str], Awaitable[Any]] | None = None,
        on_idle: Callable[[], None] | None = None,
        tmp_dir: str | None = None,
    ) -> None:
        self._fetch_audio = fetch_audio
        self._spawn = spawn or _spawn_afplay
        self._on_idle = on_idle
        self._tmp_dir = tmp_dir
        self._jobs: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=32)
        self._worker: asyncio.Task | None = None
        self._current: Any | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="ears-live-playback")

    def enqueue(self, *, audio_b64: str | None = None, audio_ref: str | None = None) -> None:
        if not audio_b64 and not audio_ref:
            return
        try:
            self._jobs.put_nowait((audio_b64 or "", audio_ref or ""))
        except asyncio.QueueFull:
            # A stuck queue is worse than a dropped old chunk: barge-in is the
            # user-visible guarantee, so keep only the freshest audio.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._jobs.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._jobs.put_nowait((audio_b64 or "", audio_ref or ""))

    async def stop(self) -> None:
        self._stopping = True
        while True:
            try:
                self._jobs.get_nowait()
            except asyncio.QueueEmpty:
                break
        current = self._current
        self._current = None
        if current is not None:
            with contextlib.suppress(Exception):
                current.terminate()
            with contextlib.suppress(Exception):
                await current.wait()
        self._stopping = False
        if self._on_idle is not None:
            self._on_idle()

    async def aclose(self) -> None:
        await self.stop()
        if self._worker is None:
            return
        with contextlib.suppress(asyncio.QueueFull):
            self._jobs.put_nowait(None)
        await self._worker
        self._worker = None

    async def _materialize(self, audio_b64: str, audio_ref: str) -> bytes | None:
        if audio_ref:
            if self._fetch_audio is None:
                LOGGER.warning("live playback has no audio fetcher for ref")
                return None
            return await self._fetch_audio(audio_ref)
        try:
            return base64.b64decode(audio_b64)
        except ValueError:
            LOGGER.warning("live playback dropped malformed audio_b64")
            return None

    async def _run(self) -> None:
        while True:
            job = await self._jobs.get()
            if job is None:
                return
            if self._stopping:
                continue
            audio_b64, audio_ref = job
            raw = await self._materialize(audio_b64, audio_ref)
            if not raw:
                continue
            with tempfile.NamedTemporaryFile(
                suffix=".wav", dir=self._tmp_dir, delete=False
            ) as handle:
                handle.write(raw)
                path = handle.name
            process: Any | None = None
            try:
                process = await self._spawn(path)
                self._current = process
                await process.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - speaking is best-effort
                LOGGER.warning("live playback failed: %s", exc)
            finally:
                if process is not None and getattr(process, "returncode", None) is None:
                    with contextlib.suppress(Exception):
                        process.terminate()
                self._current = None
                with contextlib.suppress(OSError):
                    Path(path).unlink(missing_ok=True)
            if self._jobs.empty() and self._on_idle is not None and not self._stopping:
                self._on_idle()
