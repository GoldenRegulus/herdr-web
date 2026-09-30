"""Negotiated, independently bounded compression for Full terminal output."""

from __future__ import annotations

import json
import time
import zlib
from dataclasses import dataclass
from typing import Final

FULL_OUTPUT_COMPRESSION: Final = "deflate-v1"
FULL_OUTPUT_MAX_BYTES: Final = 8 * 1024
FULL_OUTPUT_MIN_COMPRESS_BYTES: Final = 512
FULL_OUTPUT_CPU_BUDGET_SECONDS: Final = 0.002
FULL_OUTPUT_RETRY_SECONDS: Final = 1.0
FULL_OUTPUT_POOR_SAVINGS_SKIP_CHUNKS: Final = 8


@dataclass(frozen=True)
class FullOutputPacket:
    payload: bytes
    descriptor: dict[str, str | int] | None = None


def websocket_message_size(length: int) -> int:
    """Server frames are unmasked; both candidate payloads fit in 16 bits."""
    return length + (2 if length < 126 else 4)


class FullOutputEncoder:
    """Avoid compression work for small, incompressible, or expensive chunks.

    Each stream is independent and at most 8 KiB, with no extra coalescing or
    background thread (the process may fork another Full PTY later). A slow
    attempt backs off rather than repeatedly blocking the asyncio event loop.
    """

    def __init__(self) -> None:
        self.retry_at = 0.0
        self.skip_chunks = 0

    def encode(self, data: bytes) -> FullOutputPacket:
        if not 0 < len(data) <= FULL_OUTPUT_MAX_BYTES:
            raise ValueError("Full output chunk must contain 1 to 8192 bytes")
        raw = FullOutputPacket(data)
        if len(data) < FULL_OUTPUT_MIN_COMPRESS_BYTES:
            return raw
        if self.skip_chunks:
            self.skip_chunks -= 1
            return raw
        started = time.monotonic()
        if started < self.retry_at:
            return raw
        compressed = zlib.compress(data, level=1)
        finished = time.monotonic()
        if finished - started > FULL_OUTPUT_CPU_BUDGET_SECONDS:
            self.retry_at = finished + FULL_OUTPUT_RETRY_SECONDS
        descriptor: dict[str, str | int] = {
            "type": "output-deflate", "bytes": len(data)
        }
        # Starlette send_json uses compact separators. Account for that text
        # message AND both WebSocket frame headers; raw messages stay bare.
        descriptor_size = len(json.dumps(descriptor, separators=(",", ":")).encode())
        wire_size = websocket_message_size(descriptor_size) + websocket_message_size(
            len(compressed)
        )
        minimum_savings = max(64, (len(data) + 19) // 20)
        if wire_size + minimum_savings > websocket_message_size(len(data)):
            self.skip_chunks = FULL_OUTPUT_POOR_SAVINGS_SKIP_CHUNKS
            return raw
        return FullOutputPacket(compressed, descriptor)
