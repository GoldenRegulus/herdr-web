"""Bounded, lossless parser-acknowledgement flow control for Full mode."""

from __future__ import annotations

from collections import deque
from typing import Final

OUTPUT_CHUNK_BYTES: Final = 8 * 1024
OUTPUT_WINDOW_MIN_BYTES: Final = OUTPUT_CHUNK_BYTES
OUTPUT_WINDOW_INITIAL_BYTES: Final = 32 * 1024
OUTPUT_WINDOW_MAX_BYTES: Final = 256 * 1024
# Also bound accounting for tiny PTY writes, not just their byte count.
OUTPUT_WINDOW_MAX_CHUNKS: Final = 64
OUTPUT_ACK_SLOW_SECONDS: Final = 0.25
OUTPUT_ACK_EWMA_ALPHA: Final = 0.125


class FullOutputWindow:
    """Keep raw ANSI bytes in order while adapting to parser ACK latency.

    Grow by one chunk per window of cleanly acknowledged bytes, rather than
    per ACK (which would reward clients that split their acknowledgements).
    Halve on a slow round trip and wait for that flight to drain before
    probing again. A smaller window never discards bytes already in flight.
    No timing or accounting uses compressed wire sizes.
    """

    def __init__(self) -> None:
        self.window_bytes = OUTPUT_WINDOW_INITIAL_BYTES
        self.sent_bytes = 0
        self.acknowledged_bytes = 0
        self.ack_seconds: float | None = None
        self._sent: deque[tuple[int, float]] = deque()
        self._growth_bytes = 0
        self._recover_through = 0

    @property
    def inflight_bytes(self) -> int:
        return self.sent_bytes - self.acknowledged_bytes

    @property
    def inflight_chunks(self) -> int:
        return len(self._sent)

    def has_room(self, size: int) -> bool:
        return (
            0 < size <= OUTPUT_CHUNK_BYTES
            and self.inflight_bytes + size <= self.window_bytes
            and self.inflight_chunks < OUTPUT_WINDOW_MAX_CHUNKS
        )

    def note_sent(self, size: int, now: float) -> None:
        """Reserve before awaiting the send, so a fast ACK cannot race it."""
        if not self.has_room(size):
            raise ValueError("Full output exceeds its parser acknowledgement window")
        self.sent_bytes += size
        self._sent.append((self.sent_bytes, now))

    def acknowledge(self, value: object, now: float) -> bool:
        """Accept only advancing cumulative raw-byte counts we actually sent."""
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not self.acknowledged_bytes < value <= self.sent_bytes
        ):
            return False

        released_bytes = value - self.acknowledged_bytes
        self.acknowledged_bytes = value
        sample: float | None = None
        while self._sent and self._sent[0][0] <= value:
            _end, sent_at = self._sent.popleft()
            if sample is None:
                # The oldest completed chunk includes any parser queueing.
                sample = max(0.0, now - sent_at)

        # Partial ACKs free byte credit, but cannot grow the window or reset
        # the oldest chunk's deadline. Browser parser callbacks ACK whole writes.
        if sample is None:
            return True
        slow = self.ack_seconds is not None and sample > max(
            OUTPUT_ACK_SLOW_SECONDS, self.ack_seconds * 2
        )
        if self.ack_seconds is None:
            self.ack_seconds = sample
        else:
            self.ack_seconds += OUTPUT_ACK_EWMA_ALPHA * (sample - self.ack_seconds)

        if self._recover_through:
            self._growth_bytes = 0
            if value >= self._recover_through:
                self._recover_through = 0
        elif slow:
            self.window_bytes = max(OUTPUT_WINDOW_MIN_BYTES, self.window_bytes // 2)
            self._growth_bytes = 0
            # Do not repeatedly penalize the chunks queued in this same flight.
            self._recover_through = self.sent_bytes if value < self.sent_bytes else 0
        else:
            self._growth_bytes += released_bytes
            if self._growth_bytes >= self.window_bytes:
                self._growth_bytes = 0
                self.window_bytes = min(
                    OUTPUT_WINDOW_MAX_BYTES, self.window_bytes + OUTPUT_CHUNK_BYTES
                )
        return True

    def seconds_until_timeout(self, now: float, timeout: float) -> float | None:
        """Deadline follows the oldest unparsed chunk, never the latest ACK."""
        if not self._sent:
            return None
        return max(0.0, self._sent[0][1] + timeout - now)
