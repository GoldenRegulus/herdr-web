"""Socket-local, bounded bulk input with an ordered command placeholder."""

from __future__ import annotations

import asyncio
import time

INPUT_TRANSFER_CHUNK_BYTES = 16 * 1024
INPUT_TRANSFER_IDLE_SECONDS = 30.0


class InputTransfer:
    """Only the owning WebSocket can feed or cancel this transfer.

    Queue this object at header receipt, reserving ``size`` bytes. The command
    worker waits for the complete body while the receiver keeps processing
    parser acknowledgements. No partial image or paste reaches the terminal.
    """

    def __init__(
        self,
        control: dict[str, object],
        *,
        image_limit: int,
        paste_limit: int,
        image_extensions: frozenset[str],
        pane_mode: bool,
    ) -> None:
        kind = control.get("kind")
        size = control.get("size")
        limit = image_limit if kind == "clipboard-image" else paste_limit
        if (
            kind not in ("clipboard-image", "pane-paste")
            or (kind == "pane-paste" and not pane_mode)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= limit
        ):
            raise ValueError("invalid input transfer header")
        if pane_mode and (
            not isinstance(control.get("stream_id"), int)
            or isinstance(control.get("stream_id"), bool)
        ):
            raise ValueError("invalid input transfer stream")
        extension = str(control.get("extension", "")).lower()
        if kind == "clipboard-image" and extension not in image_extensions:
            raise ValueError("unsupported clipboard image format")
        self.control = {
            "type": kind,
            "size": size,
            "extension": extension,
            "stream_id": control.get("stream_id"),
        }
        self.size = size
        self.body = bytearray()
        self.ready = asyncio.Event()
        self.cancelled = False
        self.deadline = time.monotonic() + INPUT_TRANSFER_IDLE_SECONDS

    def feed(self, data: bytes) -> bool:
        if (
            self.ready.is_set()
            or not data
            or len(data) > INPUT_TRANSFER_CHUNK_BYTES
            or len(self.body) + len(data) > self.size
        ):
            raise ValueError("invalid input transfer chunk")
        self.body.extend(data)
        self.deadline = time.monotonic() + INPUT_TRANSFER_IDLE_SECONDS
        if len(self.body) == self.size:
            self.ready.set()
        return self.ready.is_set()

    def cancel(self) -> None:
        self.cancelled = True
        self.body.clear()
        self.ready.set()

    async def command(self) -> tuple[dict[str, object], bytes | None] | None:
        await self.ready.wait()
        if self.cancelled:
            return None
        body = bytes(self.body)
        self.body.clear()
        if self.control["type"] == "pane-paste":
            # Decode after assembly so UTF-8 code points may cross chunks.
            return {
                "type": "pane-paste",
                "stream_id": self.control["stream_id"],
                "text": body.decode("utf-8"),
            }, None
        return self.control, body


class InputTransferReceiver:
    """Track at most one incomplete body and enforce a chunk-idle timeout."""

    def __init__(self) -> None:
        self.pending: InputTransfer | None = None

    def start(self, transfer: InputTransfer) -> None:
        if self.pending is not None:
            raise ValueError("an input transfer is already in progress")
        self.pending = transfer

    def feed(self, data: bytes) -> bool:
        if self.pending is None:
            return False
        if self.pending.feed(data):
            self.pending = None
        return True

    def cancel(self) -> None:
        if self.pending is not None:
            self.pending.cancel()
            self.pending = None

    async def receive(self, websocket) -> dict[str, object]:
        if self.pending is None:
            return await websocket.receive()
        remaining = self.pending.deadline - time.monotonic()
        try:
            return await asyncio.wait_for(websocket.receive(), timeout=max(0, remaining))
        except asyncio.TimeoutError as error:
            raise RuntimeError("input transfer timed out") from error
