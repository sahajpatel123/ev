"""Lock-free single-producer/single-consumer PCM16 ring buffer.

The ears process keeps a fixed-size pre-roll of 16 kHz mono int16 samples so an
utterance that contains the wake word is never truncated: when VAD detects a
speech segment, the ring can supply the ``pre_roll_s`` of audio that preceded
the segment boundary.

The buffer is lock-free by construction for exactly one writer and one reader:
both sides publish monotonically increasing absolute sample counters, and the
indices are derived by masking into a power-of-two capacity. In CPython the
individual integer assignments are atomic, and the writer never overwrites
samples the reader has not consumed because the ring keeps only the newest
``capacity`` samples.
"""

from __future__ import annotations

import array
from collections.abc import Iterable


class PCM16RingBuffer:
    """Fixed-capacity mono PCM16 ring with monotonic SPSC counters."""

    def __init__(self, capacity_samples: int) -> None:
        if capacity_samples < 2:
            raise ValueError("ring capacity must be at least 2 samples")
        # Round up to a power of two so the mask is cheap and wrap is exact.
        power = 1
        while power < capacity_samples:
            power <<= 1
        self.capacity = power
        self._mask = power - 1
        self._samples: array.array = array.array("h", [0]) * power
        self._write_pos = 0
        self._read_pos = 0

    @property
    def write_pos(self) -> int:
        return self._write_pos

    @property
    def read_pos(self) -> int:
        return self._read_pos

    def __len__(self) -> int:
        return self._write_pos - self._read_pos

    def capacity_seconds(self, sample_rate: int) -> float:
        return self.capacity / max(1, sample_rate)

    def write(self, samples: Iterable[int] | array.array | bytes) -> int:
        """Append mono PCM16 samples; returns the number written.

        The writer overwrites the oldest samples once the ring is full, which
        is the desired always-on pre-roll behaviour: the most recent
        ``capacity`` samples are always available.
        """

        if isinstance(samples, (bytes, bytearray, memoryview)):
            incoming = array.array("h")
            incoming.frombytes(bytes(samples))
        else:
            incoming = array.array("h", samples)
        count = len(incoming)
        if count == 0:
            return 0
        write = self._write_pos
        for value in incoming:
            self._samples[write & self._mask] = value
            write += 1
        self._write_pos = write
        # A reader may lag arbitrarily far behind; keep the oldest samples by
        # advancing the read cursor only when it would otherwise be overwritten.
        if self._write_pos - self._read_pos > self.capacity:
            self._read_pos = self._write_pos - self.capacity
        return count

    def read_new(self) -> array.array:
        """Return all samples written since the last read (destructive)."""

        write = self._write_pos
        read = self._read_pos
        if read >= write:
            return array.array("h")
        start = read & self._mask
        count = write - read
        if start + count <= self.capacity:
            out = array.array("h", self._samples[start : start + count])
        else:
            out = array.array("h", self._samples[start:])
            out.extend(self._samples[: (start + count) & self._mask])
        self._read_pos = write
        return out

    def read_last(self, count: int) -> array.array:
        """Return the newest ``count`` retained samples without consuming them.

        Includes samples already consumed by ``read_new`` until the writer
        overwrites them. VAD pre-roll needs that earlier audio; limiting to
        unread samples made ``read_last`` empty right after ``read_new``.
        """

        retained = min(self.capacity, self._write_pos)
        count = max(0, min(count, retained))
        if count == 0:
            return array.array("h")
        start = (self._write_pos - count) & self._mask
        if start + count <= self.capacity:
            return array.array("h", self._samples[start : start + count])
        out = array.array("h", self._samples[start:])
        out.extend(self._samples[: (start + count) & self._mask])
        return out

    def snapshot(self) -> array.array:
        """Return all retained samples (non-destructive, for diagnostics)."""

        return self.read_last(self._write_pos - self._read_pos)

    def clear(self) -> None:
        self._read_pos = self._write_pos


def pcm16_bytes(samples: Iterable[int] | array.array | bytes) -> bytes:
    """Encode mono PCM16 samples as little-endian bytes."""

    if isinstance(samples, (bytes, bytearray, memoryview)):
        return bytes(samples)
    arr = samples if isinstance(samples, array.array) else array.array("h", samples)
    return arr.tobytes()
