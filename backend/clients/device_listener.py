"""EV device listener agent — a headless "ear" for the 24/7 runtime.

Runs on any always-on machine (Mac, Raspberry Pi, home server) and:

- heartbeats the runtime every N seconds so the fleet sees the device as
  online and its listener state (battery, latency, listening/sleep/off);
- polls the cross-device sync snapshot for wake-arbitration state and reports
  whether this device is the selected session device;
- participates in wake arbitration by sending a wake intent with signal,
  proximity, and priority scores;
- delivers captures to ``/v1/events`` or ``/v1/live/events`` when online and
  keeps an offline queue (with idempotency keys) that is replayed on every
  loop iteration, so no capture is lost.

The agent is intentionally small and dependency-light: it only needs httpx,
which the backend already ships. It degrades gracefully on network failures and
keeps heartbeating instead of crashing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_QUEUE_DIR = str(Path.home() / ".ev" / "listener_queue")
QUEUE_FILENAME = "pending.jsonl"
QUARANTINE_FILENAME = "quarantine.jsonl"


def _api_url() -> str:
    return os.environ.get("EV_API_URL", DEFAULT_API_URL).rstrip("/")


def _api_key() -> str:
    key = os.environ.get("EV_API_KEY", "")
    if not key:
        raise SystemExit("EV_API_KEY is not set (export EV_API_KEY=... before running)")
    return key


def _device_id() -> str:
    device = os.environ.get("EV_DEVICE_ID", "")
    if not device:
        raise SystemExit("EV_DEVICE_ID is not set (use a registered device UUID)")
    return device


def _queue_dir() -> Path:
    return Path(os.environ.get("EV_LISTENER_QUEUE_DIR", DEFAULT_QUEUE_DIR))


class DeviceListener:
    """Heartbeat + wake-arbitration + offline-first capture client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        device_id: str,
        *,
        battery_percent: float | None = None,
        queue_dir: Path | str | None = None,
    ) -> None:
        self.client = client
        self.device_id = device_id
        self.battery_percent = battery_percent
        self.queue_dir = Path(queue_dir) if queue_dir is not None else _queue_dir()

    # ------------------------------------------------------------------ #
    # Offline capture queue
    # ------------------------------------------------------------------ #

    def _queue_path(self) -> Path:
        return self.queue_dir / QUEUE_FILENAME

    def _quarantine_path(self) -> Path:
        return self.queue_dir / QUARANTINE_FILENAME

    def pending_captures(self) -> list[dict]:
        """Read locally queued captures (JSONL, oldest first)."""
        path = self._queue_path()
        if not path.exists():
            return []
        records: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def _enqueue(self, record: dict) -> dict:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        with self._queue_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return {
            "queued": True,
            "idempotency_key": record["idempotency_key"],
            "queued_at": record["queued_at"],
            "delivery": record["delivery"],
        }

    def _rewrite_queue(self, records: list[dict]) -> None:
        if records:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            self._queue_path().write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
        else:
            self._queue_path().unlink(missing_ok=True)

    def _quarantine(self, record: dict, reason: str) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            **record,
            "quarantined_at": datetime.now(UTC).isoformat(),
            "reason": reason,
        }
        with self._quarantine_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    async def capture(
        self,
        text: str | None = None,
        *,
        source: str = "listener",
        event_type: str = "note",
        privacy_level: str = "normal",
        live: bool = False,
        channel: str = "listener",
        live_kind: str = "app",
        live_event_type: str = "note",
        payload: dict | None = None,
    ) -> dict:
        """Deliver one capture, or queue it locally when offline.

        Text captures go to ``POST /v1/events`` with an ``Idempotency-Key``;
        live captures go to ``POST /v1/live/events`` as a one-event batch
        (server-side dedupe by content hash).
        """
        idempotency_key = f"listener-{uuid.uuid4()}"
        if live:
            live_body = {
                "channel": channel,
                "kind": live_kind,
                "privacy_level": privacy_level,
                "events": [
                    {
                        "event_type": live_event_type,
                        "payload": payload if payload is not None else {"text": text or ""},
                        "device_id": self.device_id,
                        "privacy_level": privacy_level,
                    }
                ],
            }
            record = {
                "idempotency_key": idempotency_key,
                "queued_at": datetime.now(UTC).isoformat(),
                "delivery": "live",
                "payload": live_body,
            }
            try:
                response = await self.client.post("/v1/live/events", json=live_body)
            except httpx.HTTPError:
                return self._enqueue(record)
            if response.status_code == 201:
                return response.json()
            if response.status_code == 409:
                return {"duplicate": True, "idempotency_key": idempotency_key}
            reason = f"HTTP {response.status_code}: {response.text[:500]}"
            self._quarantine(record, reason)
            return {"quarantined": True, "reason": reason, "idempotency_key": idempotency_key}

        event_body: dict[str, Any] = {
            "source": source,
            "event_type": event_type,
            "privacy_level": privacy_level,
            "device_id": self.device_id,
        }
        if text is not None:
            event_body["text"] = text
        if payload is not None:
            event_body["content"] = payload
        record = {
            "idempotency_key": idempotency_key,
            "queued_at": datetime.now(UTC).isoformat(),
            "delivery": "event",
            "payload": event_body,
        }
        try:
            response = await self.client.post(
                "/v1/events",
                json=event_body,
                headers={"Idempotency-Key": idempotency_key},
            )
        except httpx.HTTPError:
            return self._enqueue(record)
        if response.status_code == 201:
            return response.json()
        if response.status_code == 409:
            return {"duplicate": True, "idempotency_key": idempotency_key}
        reason = f"HTTP {response.status_code}: {response.text[:500]}"
        self._quarantine(record, reason)
        return {"quarantined": True, "reason": reason, "idempotency_key": idempotency_key}

    async def deliver_pending(self) -> dict:
        """Replay the offline queue: 201 synced, 409 duplicate dropped,
        400/422 quarantined, network failure leaves the queue intact."""
        records = self.pending_captures()
        if not records:
            return {"synced": 0, "dropped": 0, "quarantined": 0, "errors": [], "remaining": 0}
        synced = 0
        dropped = 0
        quarantined = 0
        remaining: list[dict] = []
        errors: list[str] = []
        for index, record in enumerate(records):
            key = str(record.get("idempotency_key", ""))
            try:
                if record.get("delivery") == "live":
                    response = await self.client.post(
                        "/v1/live/events", json=record["payload"]
                    )
                else:
                    response = await self.client.post(
                        "/v1/events",
                        json=record["payload"],
                        headers={"Idempotency-Key": key},
                    )
            except httpx.HTTPError as exc:
                remaining.extend(records[index:])
                errors.append(f"{key}: {exc}")
                break
            if response.status_code == 201:
                synced += 1
            elif response.status_code == 409:
                dropped += 1
            elif response.status_code in (400, 422):
                quarantined += 1
                self._quarantine(record, response.text[:500])
            else:
                remaining.extend(records[index:])
                errors.append(f"{key}: HTTP {response.status_code} {response.text[:200]}")
                break
        self._rewrite_queue(remaining)
        return {
            "synced": synced,
            "dropped": dropped,
            "quarantined": quarantined,
            "errors": errors,
            "remaining": len(remaining),
        }

    async def poll_arbitration(self) -> dict:
        """Poll the cross-device runtime snapshot for wake-arbitration state."""
        snapshot = await self.sync_state()
        runtime = snapshot.get("runtime", {})
        state = runtime.get("state", "idle")
        session_device_id = runtime.get("device_id")
        active = state in ("verifying", "awake", "processing", "responding", "follow_up")
        events = snapshot.get("events", [])
        latest_wake = next((event for event in events if event.get("kind") == "wake"), None)
        return {
            "state": state,
            "session_device_id": session_device_id,
            "selected": bool(active and session_device_id == self.device_id),
            "latest_wake": latest_wake,
        }

    async def heartbeat(
        self,
        *,
        status: str = "ok",
        listener_state: str = "listening",
        latency_ms: int | None = None,
    ) -> dict:
        """Report device liveness to the runtime."""
        payload: dict[str, Any] = {
            "device_id": self.device_id,
            "status": status,
            "listener_state": listener_state,
        }
        if self.battery_percent is not None:
            payload["battery_percent"] = self.battery_percent
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        response = await self.client.post("/v1/runtime/heartbeat", json=payload)
        response.raise_for_status()
        return response.json()

    async def wake(
        self,
        *,
        signal_score: float = 0.5,
        proximity_score: float = 0.5,
        priority: float = 0.5,
        payload: dict | None = None,
        text_hint: str | None = None,
        audio_ref: str | None = None,
        frames_b64: str | None = None,
        sample_rate: int = 16000,
    ) -> dict:
        """Send one wake intent with real wake-engine evidence.

        The runtime refuses intents that only carry client-supplied scores;
        pass ``text_hint`` (dev/test phrase), ``audio_ref``, or ``frames_b64``
        so Agent 3's engine can actually detect EVIE.
        """
        intent: dict[str, object] = {
            "device_id": self.device_id,
            "signal_score": signal_score,
            "proximity_score": proximity_score,
            "priority": priority,
            "payload": payload or {},
            "sample_rate": sample_rate,
        }
        if text_hint is not None:
            intent["text_hint"] = text_hint
        if audio_ref is not None:
            intent["audio_ref"] = audio_ref
        if frames_b64 is not None:
            intent["frames_b64"] = frames_b64
        response = await self.client.post("/v1/runtime/wake", json=[intent])
        response.raise_for_status()
        outcome = response.json()
        self.session_id = outcome.get("session_id")
        return outcome

    async def verify_owner(
        self,
        *,
        nonce: str,
        phrase: str,
        samples: list[str],
        liveness_proof: str | None = None,
        live_score: float | None = None,
        liveness_proof_path: str | None = None,
        audio_sha256: str | None = None,
    ) -> dict:
        """Complete owner speaker verification with real anti-spoof evidence.

        No hardcoded ``live`` claim: the proof comes from Agent 5's
        challenge-response (the phrase) or from a liveness proof produced by
        the capture pipeline (``liveness_proof`` or ``liveness_proof_path``).
        """
        if liveness_proof is None and liveness_proof_path:
            proof_path = Path(liveness_proof_path).expanduser()
            if not proof_path.is_file():
                raise FileNotFoundError(f"liveness proof path missing: {proof_path}")
            liveness_proof = proof_path.read_text(encoding="utf-8").strip()
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "nonce": nonce,
            "phrase": phrase,
            "samples": samples,
        }
        if liveness_proof:
            payload["liveness_proof"] = liveness_proof
        if live_score is not None:
            payload["live_score"] = live_score
        if audio_sha256:
            payload["audio_sha256"] = audio_sha256
        response = await self.client.post("/v1/runtime/verify", json=payload)
        response.raise_for_status()
        return response.json()

    async def utterance(
        self,
        *,
        text: str | None = None,
        audio_b64: str | None = None,
        follow_up: bool = False,
        reverify_token: str | None = None,
    ) -> dict:
        """Send one utterance (or a same-session follow-up) to the runtime."""
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "text": text,
            "audio_b64": audio_b64,
            "follow_up": follow_up,
            "reverify_token": reverify_token,
        }
        response = await self.client.post(
            "/v1/runtime/utterance",
            json={key: value for key, value in payload.items() if value is not None},
        )
        response.raise_for_status()
        return response.json()

    async def voice_cycle(
        self,
        *,
        phrase: str,
        samples: list[str],
        text: str,
        follow_up_text: str | None = None,
        signal_score: float = 0.9,
        proximity_score: float = 1.0,
        priority: float = 0.8,
        text_hint: str = "evie",
        liveness_proof_path: str | None = None,
    ) -> dict:
        """Run the full voice loop: wake → verify → listen → reply → follow-up."""
        wake = await self.wake(
            signal_score=signal_score,
            proximity_score=proximity_score,
            priority=priority,
            text_hint=text_hint,
        )
        if wake.get("blocked"):
            return {"blocked": True, "block_reason": wake.get("block_reason")}
        # The runtime issues the challenge at wake; the device relays it to the
        # owner and the owner's repetition is verified below.
        phrase = phrase or wake.get("challenge_phrase") or ""
        verified = await self.verify_owner(
            nonce=wake["challenge_nonce"],
            phrase=phrase,
            samples=samples,
            liveness_proof_path=liveness_proof_path,
        )
        if not verified.get("verified"):
            return verified
        reply = await self.utterance(text=text)
        if follow_up_text and reply.get("state") == "follow_up":
            follow_up = await self.utterance(text=follow_up_text, follow_up=True)
            return {"reply": reply, "follow_up": follow_up}
        return {"reply": reply}

    async def sync_state(self, since: str | None = None) -> dict:
        """Fetch the convergent cross-device runtime snapshot."""
        params: dict[str, str | int] = {"limit": 200}
        if since:
            params["since"] = since
        response = await self.client.get("/v1/runtime/sync", params=params)
        response.raise_for_status()
        return response.json()

    async def run_loop(
        self,
        *,
        interval_seconds: int = 30,
        wake_once: bool = False,
        signal_score: float = 0.5,
        proximity_score: float = 0.5,
        priority: float = 0.5,
        text_hint: str | None = None,
        max_backoff_seconds: int = 600,
    ) -> None:
        """Run the always-on listener loop.

        Every iteration: heartbeat, poll wake-arbitration state, and replay
        any offline captures. With ``wake_once`` the loop also sends one wake
        intent and exits when arbitration succeeds. Failures are offline-
        tolerant: the queue is preserved and the loop retries with a capped
        exponential backoff instead of crashing.
        """
        first = True
        consecutive_failures = 0
        while True:
            try:
                await self.heartbeat()
                print(f"[listener] heartbeat ok ({self.device_id})")
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                consecutive_failures += 1
                print(f"[listener] heartbeat failed, retrying: {exc}")
            else:
                consecutive_failures = 0

            try:
                arbitration = await self.poll_arbitration()
                print(
                    f"[listener] runtime state={arbitration['state']} "
                    f"selected={arbitration['selected']}"
                )
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                print(f"[listener] arbitration poll failed, retrying: {exc}")

            try:
                summary = await self.deliver_pending()
                if any(
                    summary[key]
                    for key in ("synced", "dropped", "quarantined", "errors")
                ):
                    print(f"[listener] queue sync: {summary}")
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                print(f"[listener] capture sync failed, queue kept: {exc}")

            if wake_once and first:
                try:
                    outcome = await self.wake(
                        signal_score=signal_score,
                        proximity_score=proximity_score,
                        priority=priority,
                        text_hint=text_hint,
                    )
                    print(f"[listener] wake outcome: {outcome.get('state')}")
                    return
                except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                    print(f"[listener] wake failed, retrying: {exc}")
            first = False
            wait = max(1, interval_seconds)
            if consecutive_failures:
                wait = min(wait * (2 ** min(consecutive_failures, 5)), max_backoff_seconds)
            await asyncio.sleep(wait)


def main() -> None:
    parser = argparse.ArgumentParser(description="EV device listener agent")
    parser.add_argument("--once", action="store_true", help="Heartbeat and exit")
    parser.add_argument("--wake-once", action="store_true", help="Heartbeat then wake once")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("EV_LISTENER_INTERVAL", "30")))
    parser.add_argument("--queue-dir", default=os.environ.get("EV_LISTENER_QUEUE_DIR", DEFAULT_QUEUE_DIR))
    parser.add_argument("--capture", default=None, help="Capture one text event (queued offline if unreachable)")
    parser.add_argument("--live-capture", default=None, help="Capture one live event (queued offline if unreachable)")
    parser.add_argument("--live-channel", default=os.environ.get("EV_LISTENER_LIVE_CHANNEL", "listener"))
    parser.add_argument("--live-kind", default=os.environ.get("EV_LISTENER_LIVE_KIND", "app"))
    parser.add_argument("--live-event-type", default=os.environ.get("EV_LISTENER_LIVE_EVENT_TYPE", "note"))
    parser.add_argument("--live-payload", default=None, help="JSON dict payload for --live-capture")
    parser.add_argument("--signal", type=float, default=0.5)
    parser.add_argument("--proximity", type=float, default=0.5)
    parser.add_argument("--priority", type=float, default=0.5)
    parser.add_argument("--text-hint", default=os.environ.get("EV_WAKE_TEXT_HINT", ""))
    parser.add_argument(
        "--liveness-proof-path",
        default=os.environ.get("EV_LIVENESS_PROOF_PATH", ""),
    )
    parser.add_argument("--battery", type=float, default=None)
    parser.add_argument("--voice-cycle", action="store_true", help="Run wake → verify → say → follow-up")
    parser.add_argument("--challenge-phrase", default=os.environ.get("EV_CHALLENGE_PHRASE", ""))
    parser.add_argument("--verify-sample", default=os.environ.get("EV_VERIFY_SAMPLE", ""))
    parser.add_argument("--say", default=os.environ.get("EV_SAY", ""))
    parser.add_argument("--follow-up-say", default=os.environ.get("EV_FOLLOW_UP_SAY", ""))
    args = parser.parse_args()

    listener = DeviceListener(
        httpx.AsyncClient(
            base_url=_api_url(),
            headers={"Authorization": f"Bearer {_api_key()}"},
            timeout=15.0,
        ),
        _device_id(),
        battery_percent=args.battery,
        queue_dir=args.queue_dir,
    )

    async def _run() -> None:
        live_payload: dict | None = None
        if args.live_payload:
            try:
                live_payload = json.loads(args.live_payload)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"--live-payload must be a JSON object: {exc}") from None

        if args.once:
            try:
                await listener.heartbeat()
                print("[listener] one-shot heartbeat ok")
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                print(f"[listener] heartbeat offline: {exc}")
            try:
                arbitration = await listener.poll_arbitration()
                state = arbitration["state"]
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                state = "unreachable"
                print(f"[listener] arbitration poll offline: {exc}")
            summary = await listener.deliver_pending()
            print(
                "[listener] one-shot ok: "
                f"state={state} queue={summary}"
            )
            if args.capture or args.live_capture:
                result = await listener.capture(
                    args.capture,
                    live=bool(args.live_capture),
                    channel=args.live_channel,
                    live_kind=args.live_kind,
                    live_event_type=args.live_event_type,
                    payload=live_payload,
                )
                print(f"[listener] capture: {result}")
            return
        if args.voice_cycle:
            if not args.challenge_phrase or not args.verify_sample or not args.say:
                raise SystemExit(
                    "--voice-cycle needs --challenge-phrase, --verify-sample (base64), --say"
                )
            result = await listener.voice_cycle(
                phrase=args.challenge_phrase,
                samples=[args.verify_sample],
                text=args.say,
                follow_up_text=args.follow_up_say or None,
                liveness_proof_path=args.liveness_proof_path or None,
            )
            print(f"[listener] voice cycle: {result}")
            return
        if args.capture or args.live_capture:
            result = await listener.capture(
                args.capture,
                live=bool(args.live_capture),
                channel=args.live_channel,
                live_kind=args.live_kind,
                live_event_type=args.live_event_type,
                payload=live_payload,
            )
            print(f"[listener] capture: {result}")
        await listener.run_loop(
            interval_seconds=args.interval,
            wake_once=args.wake_once,
            signal_score=args.signal,
            proximity_score=args.proximity,
            priority=args.priority,
            text_hint=args.text_hint or None,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
