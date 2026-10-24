"""A local, Jupyter-proxy-friendly browser terminal for Herdr sessions."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
from collections.abc import Iterator
import errno
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import pty
import secrets
import shutil
import signal
import socket
import struct
import termios
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from herdr_web.output_flow import (
    FullOutputWindow,
    OUTPUT_CHUNK_BYTES,
    OUTPUT_WINDOW_INITIAL_BYTES,
)
from herdr_web.full_output import (
    FULL_OUTPUT_COMPRESSION,
    FullOutputEncoder,
)
from herdr_web.input_transfer import (
    INPUT_TRANSFER_CHUNK_BYTES,
    InputTransfer,
    InputTransferReceiver,
)
from herdr_web.pane_stream import (
    AnsiFrame,
    PaneController,
    PaneObserver,
    PaneStream,
    PaneStreamError,
    TerminalClosed,
    open_pane_stream,
)
from herdr_web.theme import ThemeAppearance, resolve_theme

PACKAGE_DIR: Final = Path(__file__).resolve().parent
STATIC_DIR: Final = PACKAGE_DIR / "static"


def snapshot_static_directory(source: Path) -> tuple[Path, Path]:
    """Copy one immutable set of static files for this process."""
    root = Path(tempfile.mkdtemp(prefix="herdr-web-static-"))
    destination = root / "static"
    try:
        shutil.copytree(source, destination)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root, destination


STATIC_SNAPSHOT_ROOT, STATIC_SNAPSHOT_DIR = snapshot_static_directory(STATIC_DIR)
atexit.register(shutil.rmtree, STATIC_SNAPSHOT_ROOT, ignore_errors=True)
STATIC_ASSET_VERSION: Final = secrets.token_urlsafe(12)
STATIC_ASSET_PLACEHOLDER: Final = "__HERDR_WEB_ASSET_VERSION__"
NO_STORE_HEADERS: Final = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
PWA_MANIFEST: Final[dict[str, object]] = {
    "id": "/",
    "name": "Herdr Web",
    "short_name": "Herdr",
    "description": "Browser access to Herdr terminal sessions",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#181825",
    "theme_color": "#181825",
    "icons": [
        {
            "src": "/static/icons/herdr-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable",
        },
        {
            "src": "/static/icons/herdr-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ],
}
STAGED_IMAGE_DIRECTORY: Final = Path(tempfile.gettempdir()) / "herdr-web-images"
MAX_TERMINAL_DIMENSION: Final = 500
MAX_CLIPBOARD_IMAGE_BYTES: Final = 16 * 1024 * 1024
CLIPBOARD_IMAGE_WRITE_CHUNK_BYTES: Final = 64 * 1024
PTY_READ_SIZE: Final = 256 * 1024
PTY_COALESCE_SECONDS: Final = 0.002
OUTPUT_QUEUE_SIZE: Final = 1
OUTPUT_WEBSOCKET_CHUNK_BYTES: Final = OUTPUT_CHUNK_BYTES
OUTPUT_ACK_WINDOW_BYTES: Final = OUTPUT_WINDOW_INITIAL_BYTES
WEBSOCKET_HEARTBEAT_SECONDS: Final = 15
PANE_OUTPUT_FRESHNESS_BUDGET_SECONDS: Final = 1.0
PANE_RESYNC_TRIGGER_SECONDS: Final = PANE_OUTPUT_FRESHNESS_BUDGET_SECONDS / 2
# A pane may keep several frames in flight. The window grows on clean round
# trips and halves when a client falls too far behind, so throughput follows
# the link instead of the round-trip time.
PANE_WINDOW_MIN_BYTES: Final = 64 * 1024
PANE_WINDOW_MAX_BYTES: Final = 2 * 1024 * 1024
PANE_WINDOW_STEP_BYTES: Final = 32 * 1024
PANE_WINDOW_MAX_FRAMES: Final = 8
PANE_LAG_RESYNC_SECONDS: Final = 2.0
PANE_PAUSED_POLL_SECONDS: Final = 0.1
PANE_FULL_FRAME_DEFLATE_LEVEL: Final = 6
PANE_BUFFERED_FRAME_SECONDS: Final = 0.005
PANE_FULL_RESYNC_TIMEOUT_SECONDS: Final = 5.0
PANE_MAX_SEND_FRAMES_PER_SECOND: Final = 60.0
PANE_MIN_SEND_FRAMES_PER_SECOND: Final = 2.0
PANE_ACK_HEADROOM: Final = 1.25
PANE_ACK_EWMA_ALPHA: Final = 0.25
PANE_FRAME_ACK_TIMEOUT_SECONDS: Final = 60
PANE_INTERACTIVE_PRIORITY_SECONDS: Final = PANE_OUTPUT_FRESHNESS_BUDGET_SECONDS
PANE_INTERACTIVE_FRAME_WAIT_SECONDS: Final = 0.016
PANE_MAX_PRIORITY_BURST_FRAMES: Final = 4
PANE_COMMAND_QUEUE_SIZE: Final = 256
PANE_COMMAND_QUEUE_BYTES: Final = 32 * 1024 * 1024
PANE_COMMAND_DRAIN_TIMEOUT_SECONDS: Final = 5.0
PANE_INPUT_MESSAGE_BYTES: Final = 64 * 1024
PANE_CONTROL_MESSAGE_BYTES: Final = 1024 * 1024
WEBSOCKET_SEND_TIMEOUT_SECONDS: Final = 5.0
OUTPUT_ACK_TIMEOUT_SECONDS: Final = 60.0
HTTP_SESSION_IDLE_SECONDS: Final = 30
CHILD_TERMINATION_GRACE_SECONDS: Final = 1
CHILD_KILL_GRACE_SECONDS: Final = 1
SESSION_START_TIMEOUT_SECONDS: Final = 16
MAX_SESSION_NAME_BYTES: Final = 64
HERDR_API_TIMEOUT_SECONDS: Final = 5
HERDR_API_MAX_REQUEST_BYTES: Final = 1024 * 1024
HERDR_API_MAX_OUTPUT_BYTES: Final = 8 * 1024 * 1024
MAX_PANE_STREAMS: Final = 32
MAX_PANE_TEXT_PASTE_BYTES: Final = 512 * 1024
MAX_PI_SESSION_BYTES: Final = 512 * 1024 * 1024
MAX_SCRATCHPAD_BYTES: Final = 1024 * 1024
MAX_MARKDOWN_FILE_BYTES: Final = 8 * 1024 * 1024
MAX_MARKDOWN_FILES: Final = 2000
PI_STREAM_MAX_AGE_SECONDS: Final = 10 * 60
PANE_FRAME_MAGIC: Final = b"HWP1"
PANE_FRAME_HEADER: Final = struct.Struct("!4sIQBHH")
PANE_FRAME_FLAG_FULL: Final = 1
PANE_FRAME_FLAG_DEFLATE: Final = 2
PANE_DEFLATE_MINIMUM_BYTES: Final = 256
PANE_DEFLATE_COOPERATIVE_BYTES: Final = 64 * 1024
HERDR_PARENT_ENVIRONMENT_VARIABLES: Final = (
    "HERDR_ENV",
    "HERDR_SOCKET_PATH",
    "HERDR_CLIENT_SOCKET_PATH",
    "HERDR_SESSION",
    "HERDR_TAB_ID",
    "HERDR_WORKSPACE_ID",
    "HERDR_PANE_ID",
    "HERDR_RENDER_ENCODING",
    "HERDR_REMOTE_KEYBINDINGS",
    "SSH_TTY",
)
IMAGE_EXTENSIONS: Final = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
logger = logging.getLogger(__name__)
# Without a root handler this module's INFO records, including the browser
# diagnostics, are discarded and a freeze leaves no evidence.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


class PiSessionPreviewError(RuntimeError):
    """A user-facing pi preview failure with an appropriate HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


app = FastAPI(title="herdr-web", docs_url=None, redoc_url=None)
# Put the fixed per-process path before the compatibility path. Relative JS and
# CSS imports stay in this versioned directory, so a page cannot mix assets
# from two Herdr Web processes.
app.mount(
    f"/static/{STATIC_ASSET_VERSION}",
    StaticFiles(directory=STATIC_SNAPSHOT_DIR),
    name="versioned-static",
)
app.mount("/static", StaticFiles(directory=STATIC_SNAPSHOT_DIR), name="static")


def websocket_origin_is_allowed(websocket: WebSocket) -> bool:
    """Reject browser WebSockets opened by a different web origin."""
    origin = websocket.headers.get("origin")
    if origin is None:
        # Keep non-browser protocol clients compatible. Browsers always send
        # Origin for a WebSocket handshake.
        return True
    host = websocket.headers.get("host")
    if not host:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == host.casefold()


@app.middleware("http")
async def set_static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith(f"/static/{STATIC_ASSET_VERSION}/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/static/"):
        for name, value in NO_STORE_HEADERS.items():
            response.headers[name] = value
    return response


@dataclass(frozen=True)
class Backend:
    """A client protocol socket that this process is allowed to attach to."""

    id: str
    label: str
    socket_path: Path


@dataclass
class PtyClient:
    pid: int
    master_fd: int
    closed: bool = False

    def resize(self, cols: int, rows: int) -> None:
        if self.closed:
            return
        cols = max(1, min(cols, MAX_TERMINAL_DIMENSION))
        rows = max(1, min(rows, MAX_TERMINAL_DIMENSION))
        fcntl.ioctl(
            self.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        try:
            os.kill(self.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def _reap_nowait(self) -> bool:
        try:
            reaped_pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return True
        return reaped_pid == self.pid

    def _process_group_exists(self) -> bool:
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    async def close(self) -> None:
        """Stop the complete PTY process group and reap its leader."""
        if self.closed:
            return
        self.closed = True
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass

        reaped = False
        deadline = asyncio.get_running_loop().time() + CHILD_TERMINATION_GRACE_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            reaped = self._reap_nowait() or reaped
            if reaped and not self._process_group_exists():
                return
            await asyncio.sleep(0.05)

        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = asyncio.get_running_loop().time() + CHILD_KILL_GRACE_SECONDS
        while asyncio.get_running_loop().time() < kill_deadline:
            reaped = self._reap_nowait() or reaped
            if reaped and not self._process_group_exists():
                return
            await asyncio.sleep(0.01)
        logger.error("could not fully reap PTY process group %s", self.pid)


@dataclass
class BrowserSession:
    client: PtyClient
    output: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=OUTPUT_QUEUE_SIZE)
    )
    reader: asyncio.Task[None] | None = None
    closed: bool = False
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_activity = time.monotonic()


class SessionStart(BaseModel):
    backend_id: str
    cols: int = 120
    rows: int = 40


class NamedSessionStart(BaseModel):
    name: str


class TerminalInput(BaseModel):
    data_base64: str


class TerminalResize(BaseModel):
    cols: int
    rows: int


class FocusRequest(BaseModel):
    kind: Literal["workspace", "tab", "agent"]
    target_id: str


class ScratchpadUpdate(BaseModel):
    markdown: str


@dataclass(frozen=True)
class PaneStreamRequest:
    stream_id: int
    pane_id: str
    cols: int
    rows: int


class PaneStreamWindow:
    """Bytes and frames in flight for one pane stream.

    The pump reads ahead only while this window has room, so a slow client
    limits read-ahead instead of forcing one round trip per frame.
    """

    def __init__(self, stream_id: int) -> None:
        self.stream_id = stream_id
        self.window_bytes = PANE_WINDOW_MIN_BYTES
        self.inflight_bytes = 0
        self.inflight_frames = 0
        self.oldest_sent_at: float | None = None
        self.first_unacked_at: float | None = None
        self.lagged = False
        self.paused = False
        self.needs_resync = False
        self.room = asyncio.Event()
        self.room.set()

    def has_room(self) -> bool:
        if self.paused:
            return False
        return (
            self.inflight_bytes < self.window_bytes
            and self.inflight_frames < PANE_WINDOW_MAX_FRAMES
        )

    def note_sent(self, size: int, now: float) -> None:
        if self.inflight_frames == 0:
            self.first_unacked_at = now
        self.inflight_bytes += size
        self.inflight_frames += 1
        if self.oldest_sent_at is None:
            self.oldest_sent_at = now
        if not self.has_room():
            self.room.clear()

    def note_acknowledged(self, size: int, frames: int) -> None:
        self.inflight_bytes = max(0, self.inflight_bytes - size)
        self.inflight_frames = max(0, self.inflight_frames - frames)
        if self.inflight_frames == 0:
            self.oldest_sent_at = None
            self.first_unacked_at = None
            self.window_bytes = min(
                PANE_WINDOW_MAX_BYTES,
                self.window_bytes + PANE_WINDOW_STEP_BYTES,
            )
        if self.has_room():
            self.room.set()

    def is_lagging(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (
            not self.paused
            and self.oldest_sent_at is not None
            and now - self.oldest_sent_at >= PANE_LAG_RESYNC_SECONDS
        )

    def stalled_forever(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (
            self.first_unacked_at is not None
            and now - self.first_unacked_at >= PANE_FRAME_ACK_TIMEOUT_SECONDS
        )

    def release(self) -> None:
        """Drop everything in flight after a resync or a pause."""
        self.window_bytes = max(PANE_WINDOW_MIN_BYTES, self.window_bytes // 2)
        self.inflight_bytes = 0
        self.inflight_frames = 0
        self.oldest_sent_at = None
        self.first_unacked_at = None
        self.lagged = False
        self.room.set()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self.room.clear()
        else:
            self.room.set()

    async def wait_for_room(self) -> bool:
        """Wait for send room. Return false when the window stopped draining."""
        while True:
            if self.has_room():
                return True
            if self.is_lagging():
                return False
            self.room.clear()
            if self.paused:
                await self.room.wait()
                continue
            try:
                await asyncio.wait_for(self.room.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                continue


@dataclass
class PaneOutputFrame:
    stream_id: int
    frame: AnsiFrame
    acknowledged: asyncio.Event
    pacing_updated: asyncio.Event = field(default_factory=asyncio.Event)
    queued_at: float = field(default_factory=time.monotonic)
    sent_at: float | None = None
    encoded_bytes: int = 0
    discarded: bool = False


@dataclass(frozen=True)
class PaneBrowserCommand:
    value: bytes | dict[str, object] | InputTransfer
    size: int


def encode_pane_websocket_frame(
    stream_id: int, frame: AnsiFrame, *, deflate: bool
) -> bytes:
    """Encode one negotiated pane frame without changing its ANSI content."""
    flags = PANE_FRAME_FLAG_FULL if frame.full else 0
    payload = frame.bytes
    if deflate and len(payload) >= PANE_DEFLATE_MINIMUM_BYTES:
        level = PANE_FULL_FRAME_DEFLATE_LEVEL if frame.full else 1
        compressed = zlib.compress(payload, level)
        if len(compressed) < len(payload):
            payload = compressed
            flags |= PANE_FRAME_FLAG_DEFLATE
    header = PANE_FRAME_HEADER.pack(
        PANE_FRAME_MAGIC,
        stream_id,
        frame.seq,
        flags,
        frame.width,
        frame.height,
    )
    return header + payload


async def encode_pane_websocket_frame_async(
    stream_id: int, frame: AnsiFrame, *, deflate: bool
) -> bytes:
    """Compress a large frame in bounded cooperative event-loop steps."""
    if not deflate or len(frame.bytes) < PANE_DEFLATE_COOPERATIVE_BYTES:
        return encode_pane_websocket_frame(stream_id, frame, deflate=deflate)

    level = PANE_FULL_FRAME_DEFLATE_LEVEL if frame.full else 1
    compressor = zlib.compressobj(level)
    compressed_parts: list[bytes] = []
    for offset in range(0, len(frame.bytes), PANE_DEFLATE_COOPERATIVE_BYTES):
        compressed_parts.append(
            compressor.compress(
                frame.bytes[offset : offset + PANE_DEFLATE_COOPERATIVE_BYTES]
            )
        )
        await asyncio.sleep(0)
    compressed_parts.append(compressor.flush())
    compressed = b"".join(compressed_parts)
    if len(compressed) >= len(frame.bytes):
        return encode_pane_websocket_frame(stream_id, frame, deflate=False)

    flags = PANE_FRAME_FLAG_DEFLATE
    if frame.full:
        flags |= PANE_FRAME_FLAG_FULL
    return PANE_FRAME_HEADER.pack(
        PANE_FRAME_MAGIC,
        stream_id,
        frame.seq,
        flags,
        frame.width,
        frame.height,
    ) + compressed


class AdaptivePaneFramePacer:
    """Set one client's frame cadence from its parser acknowledgement time."""

    def __init__(self) -> None:
        self._minimum_interval = 1 / PANE_MAX_SEND_FRAMES_PER_SECOND
        self._maximum_interval = 1 / PANE_MIN_SEND_FRAMES_PER_SECOND
        self._smoothed_ack_seconds: float | None = None
        self._target_interval = self._minimum_interval
        self._last_sent_at: float | None = None
        self._next_send_at = 0.0
        self._expedite_generation = 0
        self._sent_expedite_generation = 0
        self._wakeup = asyncio.Event()

    @property
    def target_frames_per_second(self) -> float:
        return 1 / self._target_interval

    @property
    def target_interval_seconds(self) -> float:
        return self._target_interval

    def note_sent(self, sent_at: float) -> None:
        self._last_sent_at = sent_at
        self._sent_expedite_generation = self._expedite_generation

    def note_acknowledged(self, elapsed_seconds: float, window_frames: int = 1) -> None:
        sample = max(0.0, elapsed_seconds)
        if self._smoothed_ack_seconds is None:
            self._smoothed_ack_seconds = sample
        else:
            alpha = PANE_ACK_EWMA_ALPHA
            self._smoothed_ack_seconds = (
                alpha * sample + (1 - alpha) * self._smoothed_ack_seconds
            )
        # One acknowledgement covers the frames in flight, so the cadence
        # allows that many frames per measured round trip.
        headroom = PANE_ACK_HEADROOM / max(1, window_frames)
        self._target_interval = min(
            self._maximum_interval,
            max(
                self._minimum_interval,
                self._smoothed_ack_seconds * headroom,
            ),
        )
        if self._expedite_generation != self._sent_expedite_generation:
            self._next_send_at = 0.0
        elif self._last_sent_at is not None:
            self._next_send_at = self._last_sent_at + self._target_interval

    def expedite(self) -> None:
        """Wake a paced sender after interactive input."""
        self._expedite_generation += 1
        self._next_send_at = 0.0
        self._wakeup.set()

    async def wait(self) -> None:
        """Wait until this client can receive its next terminal frame."""
        while True:
            delay = self._next_send_at - time.monotonic()
            if delay <= 0:
                return
            self._wakeup.clear()
            delay = self._next_send_at - time.monotonic()
            if delay <= 0:
                return
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                return


class PaneFrameScheduler:
    """Keep one pending frame per stream and select frames fairly."""

    def __init__(self, stream_ids: list[int]) -> None:
        if not stream_ids:
            raise ValueError("the pane frame scheduler needs at least one stream")
        self._stream_ids = tuple(stream_ids)
        self._stream_positions = {
            stream_id: position for position, stream_id in enumerate(stream_ids)
        }
        self._pending: dict[int, PaneOutputFrame] = {}
        self._round_robin_position = -1
        self._priority_stream_id: int | None = None
        self._priority_until = 0.0
        self._priority_wait_until = 0.0
        self._priority_burst = 0
        self._wakeup = asyncio.Event()

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def notify(self) -> None:
        self._wakeup.set()

    def publish(self, item: PaneOutputFrame) -> None:
        if item.stream_id in self._pending:
            raise RuntimeError("a pane stream already has a pending frame")
        self._pending[item.stream_id] = item
        self.notify()

    def remove(self, item: PaneOutputFrame) -> None:
        if self._pending.get(item.stream_id) is item:
            self._pending.pop(item.stream_id, None)

    def prioritize(self, stream_id: int) -> None:
        if stream_id not in self._stream_positions:
            return
        now = time.monotonic()
        if self._priority_stream_id != stream_id or now >= self._priority_until:
            self._priority_wait_until = now + PANE_INTERACTIVE_FRAME_WAIT_SECONDS
            self._priority_burst = 0
        self._priority_stream_id = stream_id
        self._priority_until = now + PANE_INTERACTIVE_PRIORITY_SECONDS
        self.notify()

    def priority_wait_seconds(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if (
            self._priority_stream_id is None
            or self._priority_stream_id in self._pending
            or now >= self._priority_until
        ):
            return 0.0
        return max(0.0, self._priority_wait_until - now)

    def take_next(self, now: float | None = None) -> PaneOutputFrame | None:
        now = time.monotonic() if now is None else now
        if not self._pending:
            return None

        priority = self._priority_stream_id
        priority_is_current = priority is not None and now < self._priority_until
        background_is_ready = priority_is_current and any(
            stream_id != priority for stream_id in self._pending
        )
        if (
            priority_is_current
            and priority in self._pending
            and (
                not background_is_ready
                or self._priority_burst < PANE_MAX_PRIORITY_BURST_FRAMES
            )
        ):
            item = self._pending.pop(priority)
            self._round_robin_position = self._stream_positions[priority]
            self._priority_burst += 1
            return item

        stream_count = len(self._stream_ids)
        for offset in range(1, stream_count + 1):
            position = (self._round_robin_position + offset) % stream_count
            stream_id = self._stream_ids[position]
            if (
                priority_is_current
                and background_is_ready
                and self._priority_burst >= PANE_MAX_PRIORITY_BURST_FRAMES
                and stream_id == priority
            ):
                continue
            item = self._pending.pop(stream_id, None)
            if item is not None:
                self._round_robin_position = position
                if stream_id != priority:
                    self._priority_burst = 0
                return item
        return None

    def prepare_to_wait(self) -> None:
        self._wakeup.clear()

    async def wait_for_change(self, timeout: float) -> None:
        await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)


sessions: dict[str, BrowserSession] = {}
session_reaper: asyncio.Task[None] | None = None
staged_image_cleanup_tasks: set[asyncio.Task[None]] = set()
named_session_start_lock = asyncio.Lock()
navigation_snapshot_cache: dict[str, tuple[float, dict[str, object]]] = {}
pi_session_message_cache: dict[tuple[str, int], tuple[int, int, dict[str, object]]] = {}
pi_session_path_cache: dict[str, Path] = {}


async def reap_idle_sessions() -> None:
    while True:
        await asyncio.sleep(10)
        stale_before = time.monotonic() - HTTP_SESSION_IDLE_SECONDS
        stale_ids = [
            session_id
            for session_id, session in sessions.items()
            if session.last_activity < stale_before
        ]
        await asyncio.gather(*(close_session(session_id) for session_id in stale_ids))


@app.on_event("startup")
async def start_session_reaper() -> None:
    global session_reaper
    remove_stale_staged_images()
    session_reaper = asyncio.create_task(reap_idle_sessions())


@app.on_event("shutdown")
async def stop_sessions() -> None:
    global session_reaper
    if session_reaper is not None:
        session_reaper.cancel()
        await asyncio.gather(session_reaper, return_exceptions=True)
        session_reaper = None
    cleanup_tasks = list(staged_image_cleanup_tasks)
    for task in cleanup_tasks:
        task.cancel()
    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    remove_stale_staged_images()
    await asyncio.gather(*(close_session(session_id) for session_id in list(sessions)))


def config_dir() -> Path:
    """Match Herdr's normal Unix config location, with an explicit override."""
    return Path(
        os.environ.get("HERDR_WEB_CONFIG_DIR", os.environ.get("XDG_CONFIG_HOME", "") + "/herdr")
        if os.environ.get("HERDR_WEB_CONFIG_DIR") or os.environ.get("XDG_CONFIG_HOME")
        else Path.home() / ".config" / "herdr"
    )


def backend_id(socket_path: Path) -> str:
    return base64.urlsafe_b64encode(str(socket_path).encode()).decode().rstrip("=")


def client_socket_is_listening(socket_path: Path) -> bool:
    """Exclude stale socket files left behind by stopped Herdr sessions."""
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.settimeout(0.25)
    try:
        stream.connect(str(socket_path))
    except OSError:
        return False
    finally:
        stream.close()
    return True


def discover_backends() -> dict[str, Backend]:
    """Discover only conventional Herdr client sockets owned by this user.

    The browser never supplies a socket path. Keeping the allow-list server-side
    prevents the web app from becoming a general Unix-socket proxy.
    """
    base = config_dir()
    candidates = [("default", base / "herdr-client.sock")]
    sessions = base / "sessions"
    if sessions.is_dir():
        candidates.extend(
            (entry.name, entry / "herdr-client.sock")
            for entry in sorted(sessions.iterdir())
            if entry.is_dir()
        )

    found: dict[str, Backend] = {}
    for label, socket_path in candidates:
        try:
            if not socket_path.is_socket() or not client_socket_is_listening(socket_path):
                continue
        except OSError:
            continue
        backend = Backend(backend_id(socket_path), label, socket_path)
        found[backend.id] = backend
    return found


def herdr_binary() -> str:
    binary = os.environ.get("HERDR_BINARY") or shutil.which("herdr")
    if not binary:
        raise RuntimeError("could not find herdr; set HERDR_BINARY to its absolute path")
    return binary


def clean_herdr_environment() -> dict[str, str]:
    """Remove inherited attachment state before a new Herdr invocation."""
    environment = os.environ.copy()
    for variable in HERDR_PARENT_ENVIRONMENT_VARIABLES:
        environment.pop(variable, None)
    return environment


def backend_api_socket(backend: Backend) -> Path:
    return backend.socket_path.with_name("herdr.sock")


async def run_herdr_api(backend: Backend, *arguments: str) -> dict[str, object]:
    """Run a bounded public Herdr CLI operation for one selected backend."""
    api_socket = backend_api_socket(backend)
    if not api_socket.is_socket():
        raise RuntimeError("Herdr API socket is not available")
    environment = clean_herdr_environment()
    environment["HERDR_SOCKET_PATH"] = str(api_socket)
    process = await asyncio.create_subprocess_exec(
        herdr_binary(),
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=HERDR_API_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as error:
        process.kill()
        await process.wait()
        raise RuntimeError("Herdr API request timed out") from error
    if len(stdout) > HERDR_API_MAX_OUTPUT_BYTES:
        raise RuntimeError("Herdr API response is too large")
    if process.returncode != 0:
        message = stderr.decode(errors="replace").strip()
        raise RuntimeError(message or "Herdr API request failed")
    try:
        document = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Herdr API returned invalid JSON") from error
    if not isinstance(document, dict):
        raise RuntimeError("Herdr API returned an invalid response")
    return document


async def run_herdr_socket_api(
    backend: Backend, method: str, params: dict[str, object]
) -> dict[str, object]:
    """Send one bounded request directly to Herdr's public JSON API socket."""
    api_socket = backend_api_socket(backend)
    if not api_socket.is_socket():
        raise RuntimeError("Herdr API socket is not available")
    if not method:
        raise ValueError("Herdr API method cannot be empty")
    request_id = f"herdr-web:{secrets.token_hex(8)}"
    try:
        request = json.dumps(
            {"id": request_id, "method": method, "params": params},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as error:
        raise ValueError("Herdr API request is not valid JSON") from error
    if len(request) > HERDR_API_MAX_REQUEST_BYTES:
        raise ValueError("Herdr API request is too large")

    async def exchange() -> bytes:
        try:
            reader, writer = await asyncio.open_unix_connection(
                path=str(api_socket), limit=HERDR_API_MAX_OUTPUT_BYTES + 1
            )
        except (ConnectionError, OSError) as error:
            raise RuntimeError("could not connect to the Herdr API socket") from error
        try:
            writer.write(request)
            await writer.drain()
            try:
                response = await reader.readline()
            except ValueError as error:
                raise RuntimeError("Herdr API response is too large") from error
            if not response:
                raise RuntimeError("Herdr API returned an empty response")
            if len(response) > HERDR_API_MAX_OUTPUT_BYTES:
                raise RuntimeError("Herdr API response is too large")
            if not response.endswith(b"\n"):
                raise RuntimeError("Herdr API response is not newline-delimited")
            return response[:-1]
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    try:
        encoded_response = await asyncio.wait_for(
            exchange(), timeout=HERDR_API_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as error:
        raise RuntimeError("Herdr API request timed out") from error
    try:
        document = json.loads(encoded_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Herdr API returned invalid JSON") from error
    if not isinstance(document, dict) or document.get("id") != request_id:
        raise RuntimeError("Herdr API returned an invalid response")
    api_error = document.get("error")
    if isinstance(api_error, dict):
        message = api_error.get("message")
        raise RuntimeError(
            message if isinstance(message, str) and message else "Herdr API request failed"
        )
    if "result" not in document:
        raise RuntimeError("Herdr API returned an invalid response")
    return document


def validate_layout_rect(
    value: object,
    *,
    area: dict[str, int] | None = None,
) -> dict[str, int]:
    """Validate bounded Herdr cell geometry before browser projection."""
    if not isinstance(value, dict):
        raise RuntimeError("Herdr API returned invalid pane layout geometry")
    coordinates: dict[str, int] = {}
    for field in ("x", "y", "width", "height"):
        coordinate = value.get(field)
        if (
            not isinstance(coordinate, int)
            or isinstance(coordinate, bool)
            or coordinate < 0
            or coordinate > 65535
            or (field in {"width", "height"} and coordinate == 0)
        ):
            raise RuntimeError("Herdr API returned invalid pane layout geometry")
        coordinates[field] = coordinate
    if area is not None and (
        coordinates["x"] < area["x"]
        or coordinates["y"] < area["y"]
        or coordinates["x"] + coordinates["width"] > area["x"] + area["width"]
        or coordinates["y"] + coordinates["height"] > area["y"] + area["height"]
    ):
        raise RuntimeError("Herdr API returned pane geometry outside its layout")
    return coordinates


def project_layout_snapshot(record: dict[str, object]) -> dict[str, object]:
    """Keep only validated public geometry for one tab layout."""
    projected = {
        field: record[field]
        for field in ("workspace_id", "tab_id", "zoomed", "focused_pane_id")
        if field in record
    }
    area = validate_layout_rect(record.get("area"))
    projected["area"] = area
    panes = record.get("panes")
    projected_panes: list[dict[str, object]] = []
    if not isinstance(panes, list):
        raise RuntimeError("Herdr API returned invalid pane layout panes")
    for pane in panes:
        if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str):
            raise RuntimeError("Herdr API returned invalid pane layout pane")
        projected_panes.append(
            {
                "pane_id": pane["pane_id"],
                "focused": pane.get("focused") is True,
                "rect": validate_layout_rect(pane.get("rect"), area=area),
            }
        )
    projected["panes"] = projected_panes
    return projected


def herdr_snapshot(document: dict[str, object]) -> dict[str, object]:
    """Extract the public snapshot envelope returned by Herdr."""
    result = document.get("result")
    snapshot = result.get("snapshot") if isinstance(result, dict) else None
    if not isinstance(snapshot, dict):
        raise RuntimeError("Herdr API response does not contain a snapshot")
    return snapshot


def project_navigation_snapshot(document: dict[str, object]) -> dict[str, object]:
    """Return only the structured state needed by the browser navigation UI."""
    snapshot = herdr_snapshot(document)

    field_sets = {
        "workspaces": (
            "workspace_id", "label", "number", "focused", "active_tab_id",
            "tab_count", "pane_count", "agent_status",
        ),
        "tabs": (
            "tab_id", "workspace_id", "label", "number", "focused",
            "pane_count", "agent_status",
        ),
        "panes": (
            "pane_id", "tab_id", "workspace_id", "label", "focused",
            "agent", "agent_status", "terminal_title_stripped",
        ),
        "agents": (
            "pane_id", "tab_id", "workspace_id", "agent", "agent_status",
            "focused", "terminal_title_stripped",
        ),
    }
    projected: dict[str, object] = {
        key: snapshot.get(key)
        for key in (
            "version", "protocol", "focused_workspace_id", "focused_tab_id",
            "focused_pane_id",
        )
        if key in snapshot
    }
    for collection, fields in field_sets.items():
        records = snapshot.get(collection)
        projected[collection] = (
            [
                {field: record[field] for field in fields if field in record}
                for record in records
                if isinstance(record, dict)
            ]
            if isinstance(records, list)
            else []
        )
    layouts = snapshot.get("layouts")
    projected["layouts"] = (
        [project_layout_snapshot(record) for record in layouts if isinstance(record, dict)]
        if isinstance(layouts, list)
        else []
    )
    return projected


def validate_pane_paste_text(value: object) -> str:
    """Validate one bounded UTF-8 text Paste operation."""
    if not isinstance(value, str) or not value:
        raise ValueError("pane paste text is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("pane paste text is invalid Unicode") from error
    if size > MAX_PANE_TEXT_PASTE_BYTES:
        raise ValueError("pane paste text is too large")
    return value


def encode_pane_mouse_sequence(control: dict[str, object]) -> str:
    """Validate one structured pane mouse operation and encode SGR bytes."""
    button_code = control.get("button_code")
    column = control.get("column")
    row = control.get("row")
    action = control.get("action")
    if (
        not isinstance(button_code, int)
        or isinstance(button_code, bool)
        or not 0 <= button_code <= 30
        or (button_code & 0x03) > 2
    ):
        raise ValueError("pane mouse button code is invalid")
    if (
        not isinstance(column, int)
        or isinstance(column, bool)
        or not 1 <= column <= MAX_TERMINAL_DIMENSION
        or not isinstance(row, int)
        or isinstance(row, bool)
        or not 1 <= row <= MAX_TERMINAL_DIMENSION
    ):
        raise ValueError("pane mouse coordinates are invalid")
    if action != "click":
        raise ValueError("pane mouse action is invalid")
    prefix = f"\x1b[<{button_code};{column};{row}"
    return f"{prefix}M{prefix}m"


def parse_pane_stream_requests(
    initial: dict[str, object], snapshot: dict[str, object]
) -> tuple[str, list[PaneStreamRequest]]:
    """Validate browser stream requests against a fresh public snapshot."""
    tab_id = initial.get("tab_id")
    if not isinstance(tab_id, str) or not tab_id:
        raise ValueError("pane attachment requires a tab ID")
    raw_requests = initial.get("panes")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise ValueError("pane attachment requires at least one pane")
    if len(raw_requests) > MAX_PANE_STREAMS:
        raise ValueError(f"pane attachment is limited to {MAX_PANE_STREAMS} panes")

    panes = snapshot.get("panes")
    allowed = {
        record.get("pane_id")
        for record in panes
        if isinstance(record, dict)
        and record.get("tab_id") == tab_id
        and isinstance(record.get("pane_id"), str)
    } if isinstance(panes, list) else set()
    tabs = snapshot.get("tabs")
    tab_exists = isinstance(tabs, list) and any(
        isinstance(record, dict) and record.get("tab_id") == tab_id for record in tabs
    )
    if not tab_exists:
        raise ValueError("selected Herdr tab was not found")

    requests: list[PaneStreamRequest] = []
    stream_ids: set[int] = set()
    pane_ids: set[str] = set()
    for raw in raw_requests:
        if not isinstance(raw, dict):
            raise ValueError("pane attachment contains an invalid record")
        stream_id = raw.get("stream_id")
        pane_id = raw.get("pane_id")
        cols = raw.get("cols")
        rows = raw.get("rows")
        if (
            not isinstance(stream_id, int)
            or isinstance(stream_id, bool)
            or not 1 <= stream_id <= 0xFFFFFFFF
            or stream_id in stream_ids
        ):
            raise ValueError("pane attachment contains an invalid stream ID")
        if not isinstance(pane_id, str) or pane_id not in allowed or pane_id in pane_ids:
            raise ValueError("pane attachment contains an unavailable pane")
        if (
            not isinstance(cols, int)
            or isinstance(cols, bool)
            or not isinstance(rows, int)
            or isinstance(rows, bool)
            or not 1 <= cols <= MAX_TERMINAL_DIMENSION
            or not 1 <= rows <= MAX_TERMINAL_DIMENSION
        ):
            raise ValueError("pane attachment contains invalid terminal dimensions")
        stream_ids.add(stream_id)
        pane_ids.add(pane_id)
        requests.append(PaneStreamRequest(stream_id, pane_id, cols, rows))
    return tab_id, requests


def pi_stream_directory() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return (
        Path(runtime) / "herdr-web-pi-streams"
        if runtime
        else Path(tempfile.gettempdir()) / f"herdr-web-pi-streams-{os.getuid()}"
    )


def pi_stream_path(session_file: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(str(session_file))).hexdigest()
    return pi_stream_directory() / f"{digest}.json"


def pi_pane_mapping_path(api_socket: Path, pane_id: str) -> Path:
    identity = os.fsencode(str(api_socket)) + b"\0" + pane_id.encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return pi_stream_directory() / f"pane-{digest}.json"


def read_private_json(path: Path) -> dict[str, object] | None:
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_uid != os.getuid():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            pieces.append(block["text"])
        elif block.get("type") == "image":
            pieces.append("[Image]")
    return "\n\n".join(pieces)


def read_pi_session_messages(session_file: Path, limit: int = 10) -> dict[str, object]:
    """Read the latest user/assistant messages on a pi session's active branch."""
    bounded_limit = max(1, min(limit, 50))
    try:
        metadata = session_file.stat()
    except OSError as error:
        raise PiSessionPreviewError(404, "pi session file is no longer available") from error
    if not session_file.is_file():
        raise PiSessionPreviewError(422, "pi session path is not a regular file")
    if metadata.st_size > MAX_PI_SESSION_BYTES:
        actual_mib = metadata.st_size / (1024 * 1024)
        limit_mib = MAX_PI_SESSION_BYTES // (1024 * 1024)
        raise PiSessionPreviewError(
            413,
            f"pi session is too large to preview ({actual_mib:.1f} MiB; limit {limit_mib} MiB)",
        )
    cache_key = (str(session_file), bounded_limit)
    cached = pi_session_message_cache.get(cache_key)
    if cached is not None and cached[:2] == (metadata.st_mtime_ns, metadata.st_size):
        return cached[2]

    # Keep only tree links and displayable message content. Tool results can be
    # hundreds of megabytes and are discarded immediately after each JSON line
    # is decoded, keeping memory proportional to entry count rather than file size.
    entries: list[dict[str, object]] = []
    header: dict[str, object] | None = None
    try:
        with session_file.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"line {line_number} is not an object")
                if record.get("type") == "session" and header is None:
                    header = record
                    continue
                entry_id = record.get("id")
                if not isinstance(entry_id, str):
                    continue
                compact: dict[str, object] = {
                    "id": entry_id,
                    "parentId": record.get("parentId"),
                    "type": record.get("type"),
                }
                message = record.get("message")
                if (
                    record.get("type") == "message"
                    and isinstance(message, dict)
                    and message.get("role") in {"user", "assistant"}
                ):
                    compact["message"] = message
                    compact["timestamp"] = record.get("timestamp")
                entries.append(compact)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PiSessionPreviewError(422, "pi session file contains invalid JSONL") from error
    if header is None:
        raise PiSessionPreviewError(422, "pi session file has no session header")

    by_id = {entry["id"]: entry for entry in entries}
    branch: list[dict[str, object]] = []
    current = entries[-1] if entries else None
    visited: set[str] = set()
    while current is not None:
        entry_id = current["id"]
        if entry_id in visited:
            raise PiSessionPreviewError(422, "pi session branch contains a cycle")
        visited.add(entry_id)
        branch.append(current)
        parent_id = current.get("parentId")
        current = by_id.get(parent_id) if isinstance(parent_id, str) else None
    branch.reverse()

    messages: list[dict[str, object]] = []
    for entry in branch:
        if entry.get("type") != "message" or not isinstance(entry.get("message"), dict):
            continue
        message = entry["message"]
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        markdown = text_content(message.get("content"))
        error_message = message.get("errorMessage")
        if not markdown and role == "assistant" and isinstance(error_message, str):
            markdown = f"**Error:** {error_message}"
        if not markdown:
            continue
        messages.append(
            {
                "id": entry["id"],
                "role": role,
                "markdown": markdown,
                "timestamp": message.get("timestamp") or entry.get("timestamp"),
                "streaming": False,
            }
        )
    result: dict[str, object] = {
        "session_id": header.get("id"),
        "cwd": header.get("cwd"),
        "session_bytes": metadata.st_size,
        "messages": messages[-bounded_limit:],
    }
    pi_session_message_cache[cache_key] = (metadata.st_mtime_ns, metadata.st_size, result)
    return result


def read_pi_stream(session_file: Path) -> dict[str, object] | None:
    path = pi_stream_path(session_file)
    try:
        metadata = path.stat()
        if time.time() - metadata.st_mtime > PI_STREAM_MAX_AGE_SECONDS:
            return None
    except OSError:
        return None
    document = read_private_json(path)
    if (
        not isinstance(document, dict)
        or document.get("session_file") != str(session_file)
        or document.get("role") != "assistant"
        or not isinstance(document.get("markdown"), str)
        or not document.get("streaming")
    ):
        return None
    return {
        "id": "streaming-assistant",
        "role": "assistant",
        "markdown": document["markdown"],
        "timestamp": document.get("updated_at"),
        "streaming": True,
    }


def pi_agent_record(snapshot: dict[str, object], pane_id: str | None) -> tuple[str, list[dict[str, object]]]:
    selected_pane = pane_id or snapshot.get("focused_pane_id")
    if not isinstance(selected_pane, str) or not selected_pane:
        raise PiSessionPreviewError(404, "Herdr has no selected pane to preview")
    candidates: list[dict[str, object]] = []
    for collection in ("agents", "panes"):
        records = snapshot.get(collection)
        if isinstance(records, list):
            candidates.extend(
                record for record in records
                if isinstance(record, dict) and record.get("pane_id") == selected_pane
            )
    agent = next(
        (record.get("agent") for record in candidates if isinstance(record.get("agent"), str)),
        None,
    )
    if not isinstance(agent, str) or agent.casefold() != "pi":
        raise PiSessionPreviewError(409, "the selected Herdr pane is not running pi")
    return selected_pane, candidates


def pi_session_from_snapshot(snapshot: dict[str, object], pane_id: str | None) -> Path | None:
    _selected_pane, candidates = pi_agent_record(snapshot, pane_id)
    session_value: object = next(
        (record.get("agent_session") for record in candidates if record.get("agent_session")),
        None,
    )
    if isinstance(session_value, dict):
        session_value = session_value.get("value")
    if not isinstance(session_value, str) or not session_value:
        return None
    session_file = Path(session_value)
    if not session_file.is_absolute() or session_file.suffix != ".jsonl":
        raise PiSessionPreviewError(422, "pi published an invalid session path")
    return session_file


def pi_session_roots() -> list[Path]:
    home = Path.home()
    roots = [home / ".pi" / "agent" / "sessions"]
    try:
        roots.extend(home.glob("*/.pi/agent/sessions"))
    except OSError:
        pass
    return [root for root in roots if root.is_dir()]


def session_header(path: Path) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as source:
            header = json.loads(source.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return header if isinstance(header, dict) and header.get("type") == "session" else None


def session_header_cwd(path: Path) -> str | None:
    header = session_header(path)
    cwd = header.get("cwd") if header is not None else None
    return cwd if isinstance(cwd, str) else None


def pi_session_candidates() -> list[tuple[float, Path]]:
    candidates: list[tuple[float, Path]] = []
    for root in pi_session_roots():
        try:
            paths = root.glob("*/*.jsonl")
            for path in paths:
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                candidates.append((modified, path))
        except OSError:
            continue
    return sorted(candidates, reverse=True)


def find_recent_pi_session(cwd: str) -> Path | None:
    """Find a persisted session by exact cwd across independent pi profiles."""
    for _modified, path in pi_session_candidates():
        if session_header_cwd(path) == cwd:
            return path
    return None


def find_pi_session_by_id(session_id: str) -> Path | None:
    """Resolve an opaque pi session UUID without accepting a browser file path."""
    if not session_id or len(session_id) > 128 or not all(
        character.isalnum() or character in "-_" for character in session_id
    ):
        return None
    cached = pi_session_path_cache.get(session_id)
    if cached is not None and cached.is_file():
        header = session_header(cached)
        if header is not None and header.get("id") == session_id:
            return cached
        pi_session_path_cache.pop(session_id, None)
    for _modified, path in pi_session_candidates():
        # Pi filenames end in the session UUID, so avoid opening unrelated files.
        if session_id not in path.stem:
            continue
        header = session_header(path)
        if header is not None and header.get("id") == session_id:
            pi_session_path_cache[session_id] = path
            return path
    return None


def read_pi_pane_mapping(backend: Backend, pane_id: str) -> Path | None:
    api_socket = backend_api_socket(backend)
    document = read_private_json(pi_pane_mapping_path(api_socket, pane_id))
    if (
        document is None
        or document.get("api_socket") != str(api_socket)
        or document.get("pane_id") != pane_id
        or not isinstance(document.get("session_file"), str)
    ):
        return None
    session_file = Path(document["session_file"])
    if not session_file.is_absolute() or session_file.suffix != ".jsonl":
        return None
    return session_file


async def resolve_pi_session(backend: Backend, pane_id: str | None) -> tuple[Path, str]:
    document = await run_herdr_api(backend, "api", "snapshot")
    snapshot = herdr_snapshot(document)
    selected_pane, records = pi_agent_record(snapshot, pane_id)
    session_file = pi_session_from_snapshot(snapshot, selected_pane)
    if session_file is not None and session_file.is_file():
        return session_file, selected_pane
    mapped = read_pi_pane_mapping(backend, selected_pane)
    if mapped is not None and mapped.is_file():
        return mapped, selected_pane
    cwd = next(
        (record.get("cwd") for record in records if isinstance(record.get("cwd"), str)),
        None,
    )
    recent = find_recent_pi_session(cwd) if isinstance(cwd, str) else None
    if recent is not None:
        return recent, selected_pane
    raise PiSessionPreviewError(404, "pi has not published a persistent session for this pane")


async def navigation_snapshot(
    backend: Backend, *, refresh: bool = False
) -> dict[str, object]:
    now = time.monotonic()
    cached = navigation_snapshot_cache.get(backend.id)
    if not refresh and cached is not None and cached[0] > now:
        return cached[1]
    document = await run_herdr_api(backend, "api", "snapshot")
    snapshot = project_navigation_snapshot(document)
    navigation_snapshot_cache[backend.id] = (now + 1, snapshot)
    return snapshot


def validate_session_name(name: str) -> str:
    if not name:
        raise ValueError("session name cannot be empty")
    if name in {".", ".."}:
        raise ValueError("session name cannot be . or ..")
    if not name.isascii() or not all(
        character.isalnum() or character in "._-" for character in name
    ):
        raise ValueError(
            "session name may only contain ASCII letters, numbers, '.', '_' and '-'"
        )
    if len(name.encode("ascii")) > MAX_SESSION_NAME_BYTES:
        raise ValueError(f"session name cannot be longer than {MAX_SESSION_NAME_BYTES} bytes")
    return name


def backend_with_label(name: str) -> Backend | None:
    return next(
        (backend for backend in discover_backends().values() if backend.label == name),
        None,
    )


async def stop_session_launcher(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=CHILD_TERMINATION_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def start_named_backend(name: str) -> Backend:
    """Start a named persistent Herdr session and detach its launcher client."""
    existing = backend_with_label(name)
    if existing is not None:
        return existing

    process = await asyncio.create_subprocess_exec(
        herdr_binary(),
        "--session",
        name,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=clean_herdr_environment(),
    )
    assert process.stderr is not None
    stderr_reader = asyncio.create_task(process.stderr.read())
    try:
        deadline = asyncio.get_running_loop().time() + SESSION_START_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            backend = backend_with_label(name)
            if backend is not None:
                return backend
            if process.returncode is not None:
                error = (await stderr_reader).decode(errors="replace").strip()
                raise RuntimeError(error or "Herdr exited before the session became ready")
            await asyncio.sleep(0.05)
        raise RuntimeError("Herdr did not start the session within 16 seconds")
    finally:
        await stop_session_launcher(process)
        if not stderr_reader.done():
            stderr_reader.cancel()
        await asyncio.gather(stderr_reader, return_exceptions=True)


async def wait_for_fd(master_fd: int, *, writable: bool = False) -> None:
    """Wait for PTY readiness without using a blocked worker thread."""
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    add = loop.add_writer if writable else loop.add_reader
    remove = loop.remove_writer if writable else loop.remove_reader
    add(master_fd, mark_ready)
    try:
        await ready
    finally:
        remove(master_fd)


async def read_pty_chunk(master_fd: int) -> bytes:
    """Coalesce one short burst of PTY output into one chunk."""
    await wait_for_fd(master_fd)
    deadline = asyncio.get_running_loop().time() + PTY_COALESCE_SECONDS
    chunks: list[bytes] = []
    size = 0
    while size < PTY_READ_SIZE:
        try:
            chunk = os.read(master_fd, PTY_READ_SIZE - size)
        except BlockingIOError:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(wait_for_fd(master_fd), timeout=remaining)
            except TimeoutError:
                break
            continue
        except OSError as exc:
            if exc.errno == errno.EIO and chunks:
                break
            raise
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


async def write_pty(master_fd: int, data: bytes) -> None:
    """Preserve complete input without blocking the event loop on PTY backpressure."""
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(master_fd, remaining)
        except BlockingIOError:
            await wait_for_fd(master_fd, writable=True)
            continue
        if written <= 0:
            raise OSError("PTY write made no progress")
        remaining = remaining[written:]


async def stage_clipboard_image_async(extension: str, data: bytes) -> Path:
    """Write a bounded image in cooperative steps without starting a thread."""
    if extension not in IMAGE_EXTENSIONS:
        raise ValueError("unsupported clipboard image format")
    if not data or len(data) > MAX_CLIPBOARD_IMAGE_BYTES:
        raise ValueError("clipboard image exceeds Herdr's 16 MiB limit")
    STAGED_IMAGE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        prefix="image-", suffix=f".{extension}", dir=STAGED_IMAGE_DIRECTORY
    )
    path = Path(raw_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as image:
            for offset in range(0, len(data), CLIPBOARD_IMAGE_WRITE_CHUNK_BYTES):
                image.write(data[offset : offset + CLIPBOARD_IMAGE_WRITE_CHUNK_BYTES])
                await asyncio.sleep(0)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def remove_stale_staged_images() -> None:
    """Remove files left by a terminated bridge process."""
    if not STAGED_IMAGE_DIRECTORY.is_dir():
        return
    for path in STAGED_IMAGE_DIRECTORY.glob("image-*"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove stale clipboard image %s", path)


async def remove_staged_image_later(path: Path) -> None:
    # The client synchronously reads the path after its bracketed-paste event;
    # retain it briefly for that handoff. A shutdown cancellation also removes
    # it, and the next process start removes files left by a hard termination.
    try:
        await asyncio.sleep(30)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove clipboard image %s", path)


def schedule_staged_image_removal(path: Path) -> None:
    task = asyncio.create_task(remove_staged_image_later(path))
    staged_image_cleanup_tasks.add(task)
    task.add_done_callback(staged_image_cleanup_tasks.discard)


async def start_pane_stream(
    backend: Backend, request: PaneStreamRequest, *, control: bool
) -> PaneController | PaneObserver:
    """Open one public pane stream for a validated backend and pane."""
    environment = clean_herdr_environment()
    environment["HERDR_CLIENT_SOCKET_PATH"] = str(backend.socket_path)
    environment.setdefault("TERM", "xterm-256color")
    environment.setdefault("COLORTERM", "truecolor")
    return await open_pane_stream(
        request.pane_id,
        cols=request.cols,
        rows=request.rows,
        executable=herdr_binary(),
        environment=environment,
        control=control,
    )


def start_client(backend: Backend, cols: int, rows: int) -> PtyClient:
    """Run Herdr's existing terminal client in a PTY.

    This intentionally does not reimplement Herdr's private bincode protocol.
    The normal client receives TerminalAnsi frames from the selected client
    socket, while the browser supplies the terminal that client expects.
    """
    binary = herdr_binary()
    pid, master_fd = pty.fork()
    if pid == 0:
        # The bridge can run inside Herdr. Do not make its child client look
        # like a nested launch or bind it to the parent's pane.
        environment = clean_herdr_environment()
        environment["HERDR_CLIENT_SOCKET_PATH"] = str(backend.socket_path)
        environment["HERDR_RENDER_ENCODING"] = "terminal-ansi"
        environment.setdefault("TERM", "xterm-256color")
        environment.setdefault("COLORTERM", "truecolor")
        # Make the normal Herdr client write clipboard updates as OSC 52 to
        # this PTY, where the browser can apply them to its real clipboard.
        environment["SSH_TTY"] = "herdr-web"
        # Reuse Herdr's existing Unix remote-client file-drop path for browser
        # clipboard images. The web bridge stages the file and pastes its path.
        environment["HERDR_REMOTE_KEYBINDINGS"] = "server"
        os.execvpe(binary, [binary, "client"], environment)

    os.set_blocking(master_fd, False)
    client = PtyClient(pid, master_fd)
    client.resize(cols, rows)
    return client


@app.get("/")
async def index() -> HTMLResponse:
    # The browser derives its base path from the visible URL. This works when
    # Jupyter or SageMaker removes the proxy prefix before forwarding here.
    document = (STATIC_SNAPSHOT_DIR / "index.html").read_text(encoding="utf-8")
    document = document.replace(STATIC_ASSET_PLACEHOLDER, STATIC_ASSET_VERSION)
    return HTMLResponse(document, headers=NO_STORE_HEADERS)


@app.get("/session-preview", include_in_schema=False)
async def session_preview() -> HTMLResponse:
    document = (STATIC_SNAPSHOT_DIR / "session-preview.html").read_text(encoding="utf-8")
    document = document.replace(STATIC_ASSET_PLACEHOLDER, STATIC_ASSET_VERSION)
    return HTMLResponse(document, headers=NO_STORE_HEADERS)


@app.get("/manifest.webmanifest", include_in_schema=False)
async def web_manifest() -> JSONResponse:
    return JSONResponse(
        PWA_MANIFEST,
        headers=NO_STORE_HEADERS,
        media_type="application/manifest+json",
    )


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/client-report", include_in_schema=False)
async def client_report(request: Request) -> dict[str, bool]:
    """Record a bounded browser diagnostic in the server log."""
    try:
        payload = await request.json()
    except Exception:
        return {"recorded": False}
    if not isinstance(payload, dict):
        return {"recorded": False}
    kind = str(payload.get("kind", ""))[:32]
    detail = str(payload.get("detail", ""))[:300]
    logger.info("client report: %s %s", kind, detail)
    return {"recorded": True}


@app.get("/api/backends")
async def list_backends() -> dict[str, list[dict[str, str]]]:
    return {
        "backends": [
            {"id": backend.id, "label": backend.label}
            for backend in discover_backends().values()
        ]
    }


def pi_message_response(session_file: Path, limit: int) -> dict[str, object]:
    persisted = read_pi_session_messages(session_file, limit)
    # Session parsing results are cached. Build a fresh response and message
    # list so a live overlay cannot accumulate in that cached object each poll.
    result = {**persisted, "messages": list(persisted["messages"])}
    stream = read_pi_stream(session_file)
    if stream is not None:
        messages = result["messages"]
        assert isinstance(messages, list)
        messages.append(stream)
        result["messages"] = messages[-max(1, min(limit, 50)):]
    result["live"] = stream is not None
    return result


def markdown_file_id(relative_path: Path) -> str:
    return hashlib.sha256(relative_path.as_posix().encode("utf-8")).hexdigest()[:24]


def project_markdown_files(cwd: str) -> list[dict[str, object]]:
    root = Path(cwd).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise PiSessionPreviewError(404, "project directory is no longer available")
    ignored = {".git", ".hg", ".svn", ".venv", "node_modules", "__pycache__"}
    files: list[dict[str, object]] = []
    try:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = sorted(
                name for name in directory_names
                if name not in ignored and not (Path(directory) / name).is_symlink()
            )
            for file_name in sorted(file_names):
                path = Path(directory) / file_name
                relative = path.relative_to(root)
                if path.is_symlink() or path.suffix.casefold() not in {".md", ".markdown"}:
                    continue
                metadata = path.stat()
                files.append({
                    "id": markdown_file_id(relative),
                    "name": path.name,
                    "location": relative.parent.as_posix() if relative.parent != Path(".") else "",
                    "bytes": metadata.st_size,
                    "modified_at": int(metadata.st_mtime * 1000),
                })
                if len(files) >= MAX_MARKDOWN_FILES:
                    break
            if len(files) >= MAX_MARKDOWN_FILES:
                break
    except OSError as error:
        raise PiSessionPreviewError(503, f"could not scan project Markdown files: {error}") from error
    files.sort(key=lambda item: (str(item["location"]).casefold(), str(item["name"]).casefold()))
    return files


def resolve_project_markdown_file(cwd: str, file_id: str) -> tuple[Path, Path]:
    if len(file_id) != 24 or any(character not in "0123456789abcdef" for character in file_id):
        raise PiSessionPreviewError(404, "Markdown file was not found")
    root = Path(cwd).expanduser().resolve(strict=False)
    for item in project_markdown_files(cwd):
        relative = Path(str(item["location"])) / str(item["name"])
        if item["id"] == file_id:
            path = (root / relative).resolve(strict=False)
            if path.parent != root and root not in path.parents:
                break
            return path, relative
    raise PiSessionPreviewError(404, "Markdown file was not found")


def read_project_markdown_file(cwd: str, file_id: str) -> dict[str, object]:
    path, relative = resolve_project_markdown_file(cwd, file_id)
    try:
        metadata = path.stat()
        if metadata.st_size > MAX_MARKDOWN_FILE_BYTES:
            raise PiSessionPreviewError(413, "Markdown file exceeds the 8 MiB preview limit")
        markdown = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise PiSessionPreviewError(422, "Markdown file is not valid UTF-8") from error
    except OSError as error:
        raise PiSessionPreviewError(503, f"could not read Markdown file: {error}") from error
    return {
        "file_id": file_id,
        "name": relative.name,
        "location": relative.parent.as_posix() if relative.parent != Path(".") else "",
        "bytes": metadata.st_size,
        "markdown": markdown,
    }


def scratchpad_path(cwd: str) -> Path:
    """Return private, project-keyed note storage outside the project tree."""
    identity = str(Path(cwd).expanduser().resolve(strict=False))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return config_dir() / "scratchpads" / f"{digest}.md"


def read_scratchpad(cwd: str) -> str:
    path = scratchpad_path(cwd)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as error:
        raise PiSessionPreviewError(422, "project scratchpad is not valid UTF-8") from error
    if len(content.encode("utf-8")) > MAX_SCRATCHPAD_BYTES:
        raise PiSessionPreviewError(413, "project scratchpad is too large")
    return content


def write_scratchpad(cwd: str, markdown: str) -> None:
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_SCRATCHPAD_BYTES:
        raise PiSessionPreviewError(413, "project scratchpad exceeds the 1 MiB limit")
    path = scratchpad_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("wb") as output:
            os.chmod(temporary, 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pi_session_cwd(session_id: str) -> str:
    session_file = find_pi_session_by_id(session_id)
    if session_file is None:
        raise HTTPException(status_code=404, detail="pi session is no longer available")
    try:
        cwd = read_pi_session_messages(session_file, 1).get("cwd")
    except PiSessionPreviewError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=f"could not inspect pi session: {error}") from error
    if not isinstance(cwd, str) or not cwd:
        raise HTTPException(status_code=422, detail="pi session has no project working directory")
    return cwd


@app.get("/api/pi-sessions/{session_id}/markdown-files")
async def get_pi_session_markdown_files(session_id: str) -> dict[str, object]:
    cwd = pi_session_cwd(session_id)
    try:
        return {"files": project_markdown_files(cwd)}
    except PiSessionPreviewError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@app.get("/api/pi-sessions/{session_id}/markdown-files/{file_id}")
async def get_pi_session_markdown_file(session_id: str, file_id: str) -> dict[str, object]:
    cwd = pi_session_cwd(session_id)
    try:
        document = read_project_markdown_file(cwd, file_id)
    except PiSessionPreviewError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    return {
        "session_id": session_id,
        "cwd": document["location"] or "Project root",
        "session_bytes": document["bytes"],
        "document_name": document["name"],
        "document_mode": True,
        "messages": [{
            "id": f"markdown-{file_id}",
            "role": "document",
            "markdown": document["markdown"],
            "timestamp": None,
            "streaming": False,
        }],
        "live": False,
    }


@app.get("/api/pi-sessions/{session_id}/scratchpad")
async def get_pi_session_scratchpad(session_id: str) -> dict[str, object]:
    cwd = pi_session_cwd(session_id)
    try:
        markdown = read_scratchpad(cwd)
    except PiSessionPreviewError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=503, detail=f"could not read project scratchpad: {error}") from error
    return {"markdown": markdown, "project_key": hashlib.sha256(cwd.encode()).hexdigest()[:12]}


@app.put("/api/pi-sessions/{session_id}/scratchpad")
async def put_pi_session_scratchpad(
    session_id: str, update: ScratchpadUpdate
) -> dict[str, object]:
    cwd = pi_session_cwd(session_id)
    try:
        write_scratchpad(cwd, update.markdown)
    except PiSessionPreviewError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=503, detail=f"could not save project scratchpad: {error}") from error
    return {"saved": True, "bytes": len(update.markdown.encode("utf-8"))}


@app.get("/api/pi-sessions/{session_id}/messages")
async def get_pi_session_messages(session_id: str, limit: int = 10) -> dict[str, object]:
    session_file = find_pi_session_by_id(session_id)
    if session_file is None:
        raise HTTPException(status_code=404, detail="pi session is no longer available")
    try:
        return pi_message_response(session_file, limit)
    except PiSessionPreviewError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=f"could not inspect pi session: {error}") from error


@app.get("/api/backends/{backend_id}/pi-messages")
async def get_pi_messages(
    backend_id: str, pane_id: str | None = None, limit: int = 10
) -> dict[str, object]:
    backend = discover_backends().get(backend_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="Herdr backend is no longer available")
    try:
        session_file, selected_pane = await resolve_pi_session(backend, pane_id)
        result = pi_message_response(session_file, limit)
        result["pane_id"] = selected_pane
        session_id = result.get("session_id")
        if isinstance(session_id, str):
            pi_session_path_cache[session_id] = session_file
        return result
    except PiSessionPreviewError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=f"could not inspect pi session: {error}") from error


@app.get("/api/backends/{backend_id}/navigation")
async def get_backend_navigation(backend_id: str) -> dict[str, object]:
    backend = discover_backends().get(backend_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="Herdr backend is no longer available")
    try:
        return await navigation_snapshot(backend)
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/backends/{backend_id}/focus")
async def focus_backend_target(
    backend_id: str, request: FocusRequest
) -> dict[str, object]:
    backend = discover_backends().get(backend_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="Herdr backend is no longer available")
    try:
        snapshot = await navigation_snapshot(backend, refresh=True)
        collection = "agents" if request.kind == "agent" else f"{request.kind}s"
        id_field = "pane_id" if request.kind == "agent" else f"{request.kind}_id"
        records = snapshot.get(collection)
        allowed_ids = (
            {
                record.get(id_field)
                for record in records
                if isinstance(record, dict) and isinstance(record.get(id_field), str)
            }
            if isinstance(records, list)
            else set()
        )
        if request.target_id not in allowed_ids:
            raise HTTPException(status_code=404, detail=f"Herdr {request.kind} was not found")
        await run_herdr_api(backend, request.kind, "focus", request.target_id)
        navigation_snapshot_cache.pop(backend.id, None)
        return await navigation_snapshot(backend, refresh=True)
    except HTTPException:
        raise
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/theme")
async def get_theme(appearance: ThemeAppearance = "dark") -> dict[str, object]:
    return resolve_theme(appearance)


@app.post("/api/backends")
async def create_named_backend(request: NamedSessionStart) -> dict[str, dict[str, str]]:
    try:
        name = validate_session_name(request.name)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        async with named_session_start_lock:
            backend = await start_named_backend(name)
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"backend": {"id": backend.id, "label": backend.label}}


def websocket_output_chunks(data: bytes) -> Iterator[bytes]:
    """Split ordered PTY output into acknowledgement-sized WebSocket frames."""
    for offset in range(0, len(data), OUTPUT_WEBSOCKET_CHUNK_BYTES):
        yield data[offset : offset + OUTPUT_WEBSOCKET_CHUNK_BYTES]


async def read_pty(session: BrowserSession) -> None:
    try:
        while True:
            try:
                data = await read_pty_chunk(session.client.master_fd)
            except OSError:
                return
            if not data:
                return
            for chunk in websocket_output_chunks(data):
                await session.output.put(chunk)
    finally:
        session.closed = True


async def close_session(session_id: str) -> None:
    session = sessions.pop(session_id, None)
    if session is None:
        return
    if session.reader is not None:
        session.reader.cancel()
        await asyncio.gather(session.reader, return_exceptions=True)
    await session.client.close()


def session_or_404(session_id: str) -> BrowserSession:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="terminal session not found")
    return session


@app.post("/api/sessions")
async def create_session(request: SessionStart) -> dict[str, str]:
    backend = discover_backends().get(request.backend_id)
    if backend is None:
        raise HTTPException(status_code=404, detail="Herdr backend is no longer available")
    try:
        client = start_client(backend, request.cols, request.rows)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    session = BrowserSession(client=client)
    session.reader = asyncio.create_task(read_pty(session))
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = session
    return {"id": session_id, "label": backend.label}


@app.get("/api/sessions/{session_id}/read")
async def read_session(session_id: str) -> dict[str, object]:
    """Long-poll terminal output so this also works behind HTTP-only proxies."""
    session = session_or_404(session_id)
    session.touch()
    chunks: list[bytes] = []
    try:
        chunks.append(await asyncio.wait_for(session.output.get(), timeout=1))
    except asyncio.TimeoutError:
        pass
    while sum(map(len, chunks)) < OUTPUT_WEBSOCKET_CHUNK_BYTES:
        try:
            chunks.append(session.output.get_nowait())
        except asyncio.QueueEmpty:
            break
    data = b"".join(chunks)
    return {
        "data_base64": base64.b64encode(data).decode(),
        "closed": session.closed and session.output.empty(),
    }


@app.post("/api/sessions/{session_id}/input")
async def send_input(session_id: str, request: TerminalInput) -> dict[str, bool]:
    session = session_or_404(session_id)
    session.touch()
    try:
        data = base64.b64decode(request.data_base64, validate=True)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="input is not valid base64") from error
    try:
        await write_pty(session.client.master_fd, data)
    except OSError as error:
        raise HTTPException(status_code=410, detail="terminal session is closed") from error
    return {"ok": True}


@app.post("/api/sessions/{session_id}/resize")
async def resize_session(session_id: str, request: TerminalResize) -> dict[str, bool]:
    session = session_or_404(session_id)
    session.touch()
    session.client.resize(request.cols, request.rows)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    await close_session(session_id)
    return {"ok": True}


async def run_panes_websocket(
    websocket: WebSocket,
    backend: Backend,
    initial: dict[str, object],
) -> None:
    """Multiplex one active tab's public terminal-session streams."""
    try:
        snapshot = await navigation_snapshot(backend, refresh=True)
        tab_id, requests = parse_pane_stream_requests(initial, snapshot)
    except (OSError, RuntimeError, ValueError) as error:
        await websocket.close(code=4400, reason=str(error)[:120])
        return

    pane_deflate_enabled = initial.get("compression") == "deflate"
    requests_by_stream = {request.stream_id: request for request in requests}
    clients: dict[int, PaneStream] = {}
    modes: dict[int, str] = {}
    stream_sizes = {
        request.stream_id: (request.cols, request.rows) for request in requests
    }
    stream_resize_locks = {
        request.stream_id: asyncio.Lock() for request in requests
    }
    writable_streams: set[int] = set()
    all_clients: set[PaneStream] = set()
    try:
        for request in requests:
            client = await start_pane_stream(backend, request, control=True)
            clients[request.stream_id] = client
            modes[request.stream_id] = "control"
            all_clients.add(client)
    except (OSError, RuntimeError) as error:
        await asyncio.gather(*(client.close() for client in all_clients))
        await websocket.send_json({"type": "error", "message": str(error)})
        return

    attached_message = {
        "type": "panes-attached",
        "input_chunk_bytes": INPUT_TRANSFER_CHUNK_BYTES,
        "tab_id": tab_id,
        "compression": "deflate" if pane_deflate_enabled else None,
        "streams": [
            {
                "stream_id": request.stream_id,
                "pane_id": request.pane_id,
                "mode": "control",
            }
            for request in requests
        ],
    }
    try:
        await asyncio.wait_for(
            websocket.send_json(attached_message),
            timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await asyncio.gather(*(client.close() for client in all_clients))
        try:
            await websocket.close(code=1011, reason="pane WebSocket send timed out")
        except RuntimeError:
            pass
        return
    except (OSError, RuntimeError):
        await asyncio.gather(*(client.close() for client in all_clients))
        return

    control_output: asyncio.Queue[dict[str, object] | None] = asyncio.Queue(
        maxsize=OUTPUT_QUEUE_SIZE
    )
    command_queue: asyncio.Queue[PaneBrowserCommand | None] = asyncio.Queue(
        maxsize=PANE_COMMAND_QUEUE_SIZE
    )
    acknowledgements: dict[tuple[int, int], PaneOutputFrame] = {}
    stream_windows: dict[int, PaneStreamWindow] = {
        request.stream_id: PaneStreamWindow(request.stream_id)
        for request in requests
    }
    fatal_error: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    frame_pacer = AdaptivePaneFramePacer()
    frame_scheduler = PaneFrameScheduler(
        [request.stream_id for request in requests]
    )
    active_stream_id = requests[0].stream_id
    pane_image_paths: set[Path] = set()
    pending_error: dict[str, object] | None = None
    no_control = object()
    resync_budget_lock = asyncio.Lock()
    next_resync_at = 0.0
    queued_command_bytes = 0

    async def queue_control(message: dict[str, object] | None) -> None:
        await control_output.put(message)
        frame_scheduler.notify()

    def mark_interactive(stream_id: int) -> None:
        frame_scheduler.prioritize(stream_id)
        frame_pacer.expedite()

    def enqueue_browser_command(
        command: bytes | dict[str, object] | InputTransfer, size: int
    ) -> bool:
        nonlocal queued_command_bytes
        if size < 0 or queued_command_bytes + size > PANE_COMMAND_QUEUE_BYTES:
            if not fatal_error.done():
                fatal_error.set_result("pane command queue is full")
            return False
        try:
            command_queue.put_nowait(PaneBrowserCommand(command, size))
        except asyncio.QueueFull:
            if not fatal_error.done():
                fatal_error.set_result("pane command queue is full")
            return False
        queued_command_bytes += size
        return True

    async def wait_for_resync_budget() -> None:
        nonlocal next_resync_at
        async with resync_budget_lock:
            delay = next_resync_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            started_at = time.monotonic()
            next_resync_at = started_at + frame_pacer.target_interval_seconds

    async def read_fresh_pane_frame(
        request: PaneStreamRequest, client: PaneStream
    ) -> tuple[PaneStream, AnsiFrame | TerminalClosed]:
        """Skip superseded framed updates and return a new full frame."""
        stream_id = request.stream_id
        await wait_for_resync_budget()
        async with stream_resize_locks[stream_id]:
            if isinstance(client, PaneController):
                cols, rows = stream_sizes[stream_id]
                await client.resize(cols, rows)
            else:
                await client.close()
                all_clients.discard(client)
                cols, rows = stream_sizes[stream_id]
                fresh_request = PaneStreamRequest(
                    stream_id=stream_id,
                    pane_id=request.pane_id,
                    cols=cols,
                    rows=rows,
                )
                client = await start_pane_stream(backend, fresh_request, control=False)
                clients[stream_id] = client
                all_clients.add(client)

            deadline = time.monotonic() + PANE_FULL_RESYNC_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                record = await asyncio.wait_for(client.read_record(), timeout=remaining)
                if isinstance(record, TerminalClosed):
                    return client, record
                if record.full and (record.width, record.height) == (cols, rows):
                    return client, record

    def discard_stream_frames(stream_id: int) -> None:
        """Drop frames that are still in flight for one stream."""
        for key in [key for key in acknowledgements if key[0] == stream_id]:
            item = acknowledgements.pop(key)
            item.discarded = True
            item.pacing_updated.set()
            item.acknowledged.set()
            frame_scheduler.remove(item)
        stream_windows[stream_id].release()

    async def pump_stream(request: PaneStreamRequest) -> None:
        stream_id = request.stream_id
        client = clients[stream_id]
        window = stream_windows[stream_id]
        received_frame = False
        pending_record: AnsiFrame | TerminalClosed | None = None
        backlog_started_at: float | None = None
        while True:
            try:
                if window.paused:
                    # Do not send while the pane is hidden. Keep draining the
                    # stream so Herdr never blocks on this observer, and resume
                    # from one full frame.
                    pending_record = None
                    window.needs_resync = True
                    try:
                        await asyncio.wait_for(
                            client.read_record(), timeout=PANE_BUFFERED_FRAME_SECONDS
                        )
                    except asyncio.TimeoutError:
                        pass
                    if window.paused:
                        await asyncio.sleep(PANE_PAUSED_POLL_SECONDS)
                    continue
                if window.needs_resync:
                    window.needs_resync = False
                    client, pending_record = await read_fresh_pane_frame(request, client)
                    continue
                if pending_record is None:
                    if not await window.wait_for_room():
                        if window.stalled_forever():
                            if not fatal_error.done():
                                fatal_error.set_result(
                                    "pane parser acknowledgement timed out"
                                )
                            return
                        # The client fell behind the window. Drop what is in
                        # flight and continue from a fresh full frame.
                        discard_stream_frames(stream_id)
                        window.needs_resync = True
                        continue
                    record = await client.read_record()
                else:
                    record = pending_record
                    pending_record = None
            except Exception as error:
                await queue_control(
                    {"type": "pane-closed", "stream_id": stream_id, "reason": str(error)}
                )
                return
            if isinstance(record, TerminalClosed):
                writable_streams.discard(stream_id)
                conflict = (
                    modes[stream_id] == "control"
                    and not received_frame
                    and "already has an attached client" in record.reason.casefold()
                )
                if conflict:
                    await client.close()
                    all_clients.discard(client)
                    try:
                        client = await start_pane_stream(backend, request, control=False)
                    except (OSError, RuntimeError) as error:
                        await queue_control(
                            {
                                "type": "pane-closed",
                                "stream_id": stream_id,
                                "reason": str(error),
                            }
                        )
                        return
                    clients[stream_id] = client
                    all_clients.add(client)
                    modes[stream_id] = "observe"
                    await queue_control(
                        {"type": "pane-mode", "stream_id": stream_id, "mode": "observe"}
                    )
                    received_frame = False
                    continue
                await queue_control(
                    {
                        "type": "pane-closed",
                        "stream_id": stream_id,
                        "reason": record.reason,
                    }
                )
                return

            received_frame = True
            if modes[stream_id] == "control":
                writable_streams.add(stream_id)
            item = PaneOutputFrame(stream_id, record, asyncio.Event())
            acknowledgements[(stream_id, record.seq)] = item
            frame_scheduler.publish(item)
            try:
                # Wait until this frame leaves the send queue. The window, not
                # this frame's acknowledgement, bounds the next read.
                await item.pacing_updated.wait()
            finally:
                frame_scheduler.remove(item)
            if item.discarded:
                acknowledgements.pop((stream_id, record.seq), None)
                backlog_started_at = None
                window.needs_resync = True
                continue

            # A pane that already has a buffered successor is behind. Prefer the
            # current full state over stale history once that backlog ages.
            buffered_successor = False
            if not window.paused:
                try:
                    pending_record = await asyncio.wait_for(
                        client.read_record(), timeout=PANE_BUFFERED_FRAME_SECONDS
                    )
                    buffered_successor = isinstance(pending_record, AnsiFrame)
                except asyncio.TimeoutError:
                    pending_record = None
                except (
                    OSError,
                    RuntimeError,
                    PaneStreamError,
                ) as error:
                    await queue_control(
                        {
                            "type": "pane-closed",
                            "stream_id": stream_id,
                            "reason": str(error),
                        }
                    )
                    return
            if not buffered_successor:
                backlog_started_at = None
                continue
            if backlog_started_at is None:
                backlog_started_at = item.sent_at or item.queued_at
            if (
                time.monotonic() - backlog_started_at
                < PANE_RESYNC_TRIGGER_SECONDS
            ):
                continue
            backlog_started_at = None
            pending_record = None
            discard_stream_frames(stream_id)
            window.needs_resync = True

    def pane_frame_acknowledgement_expired() -> bool:
        return any(window.stalled_forever() for window in stream_windows.values())

    async def send_browser_output() -> None:
        nonlocal pending_error
        while True:
            if pane_frame_acknowledgement_expired():
                if not fatal_error.done():
                    fatal_error.set_result(
                        "pane parser acknowledgement timed out"
                    )
                return
            if pending_error is not None:
                control_item = pending_error
                pending_error = None
            else:
                try:
                    control_item = control_output.get_nowait()
                except asyncio.QueueEmpty:
                    control_item = no_control
            if control_item is None:
                return
            if control_item is not no_control:
                try:
                    await asyncio.wait_for(
                        websocket.send_json(control_item),
                        timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    if not fatal_error.done():
                        fatal_error.set_result("pane WebSocket send timed out")
                    return
                continue

            priority_delay = frame_scheduler.priority_wait_seconds()
            if priority_delay > 0:
                await asyncio.sleep(priority_delay)
                continue
            if frame_scheduler.has_pending:
                await frame_pacer.wait()
                # A control transition, such as control-to-observe fallback,
                # must reach the browser before that stream's next frame.
                if pending_error is not None or not control_output.empty():
                    continue
                item = frame_scheduler.take_next()
                if item is None:
                    continue
                if (
                    time.monotonic() - item.queued_at
                    >= PANE_RESYNC_TRIGGER_SECONDS
                ):
                    item.discarded = True
                    item.pacing_updated.set()
                    item.acknowledged.set()
                    continue
                encoded = await encode_pane_websocket_frame_async(
                    item.stream_id,
                    item.frame,
                    deflate=pane_deflate_enabled,
                )
                if (
                    time.monotonic() - item.queued_at
                    >= PANE_RESYNC_TRIGGER_SECONDS
                ):
                    item.discarded = True
                    item.pacing_updated.set()
                    item.acknowledged.set()
                    continue
                item.sent_at = time.monotonic()
                frame_pacer.note_sent(item.sent_at)
                try:
                    await asyncio.wait_for(
                        websocket.send_bytes(encoded),
                        timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    if not fatal_error.done():
                        fatal_error.set_result("pane WebSocket send timed out")
                    item.pacing_updated.set()
                    item.acknowledged.set()
                    return
                # The pane window bounds read-ahead. Keep several frames in
                # flight so throughput follows the link, not the round trip.
                item.encoded_bytes = len(encoded)
                stream_windows[item.stream_id].note_sent(
                    item.encoded_bytes, item.sent_at
                )
                item.pacing_updated.set()
                continue

            frame_scheduler.prepare_to_wait()
            if (
                pending_error is not None
                or not control_output.empty()
                or frame_scheduler.has_pending
            ):
                continue
            # Wake in time to fail a client that stopped acknowledging frames.
            wait_seconds = WEBSOCKET_HEARTBEAT_SECONDS
            now = time.monotonic()
            for window in stream_windows.values():
                if window.first_unacked_at is None:
                    continue
                remaining = PANE_FRAME_ACK_TIMEOUT_SECONDS - (
                    now - window.first_unacked_at
                )
                wait_seconds = min(wait_seconds, max(0.01, remaining))
            try:
                await frame_scheduler.wait_for_change(wait_seconds)
            except asyncio.TimeoutError:
                if pane_frame_acknowledgement_expired():
                    continue
                try:
                    await asyncio.wait_for(
                        websocket.send_json({"type": "ping"}),
                        timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    if not fatal_error.done():
                        fatal_error.set_result("pane WebSocket send timed out")
                    return

    def acknowledge_pane_frames(stream_id: int, seq: int) -> None:
        """Release every acknowledged frame up to this sequence number."""
        released_keys = [
            key
            for key in acknowledgements
            if key[0] == stream_id and key[1] <= seq
        ]
        if not released_keys:
            return
        window = stream_windows[stream_id]
        released_bytes = 0
        released_frames = 0
        newest_sent_at: float | None = None
        for key in released_keys:
            item = acknowledgements.pop(key)
            item.acknowledged.set()
            if item.sent_at is None:
                continue
            released_bytes += item.encoded_bytes
            released_frames += 1
            if newest_sent_at is None or item.sent_at > newest_sent_at:
                newest_sent_at = item.sent_at
        window.note_acknowledged(released_bytes, released_frames)
        if released_frames and newest_sent_at is not None:
            frame_pacer.note_acknowledged(
                time.monotonic() - newest_sent_at, released_frames
            )

    async def report_error(message: str) -> None:
        nonlocal pending_error
        error = {"type": "error", "message": message}
        try:
            control_output.put_nowait(error)
            frame_scheduler.notify()
        except asyncio.QueueFull:
            # The latest error is sufficient for the browser toast. ACK intake
            # remains independent from this bounded control-output queue.
            pending_error = error
            frame_scheduler.notify()

    transfers = InputTransferReceiver()

    async def receive_browser_messages() -> None:
        """Receive ACKs immediately and queue ordered terminal commands."""
        expected_image_bytes: int | None = None
        while True:
            message = await transfers.receive(websocket)
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                if transfers.feed(data):
                    continue
                if expected_image_bytes is None and len(data) > PANE_INPUT_MESSAGE_BYTES:
                    if not fatal_error.done():
                        fatal_error.set_result("pane input message is too large")
                    return
                if expected_image_bytes is not None and len(data) > MAX_CLIPBOARD_IMAGE_BYTES:
                    if not fatal_error.done():
                        fatal_error.set_result("clipboard image is too large")
                    return
                expected_image_bytes = None
                if not enqueue_browser_command(data, len(data)):
                    return
                continue
            text = message.get("text")
            if text is None:
                continue
            text_size = len(text.encode("utf-8"))
            if text_size > PANE_CONTROL_MESSAGE_BYTES:
                if not fatal_error.done():
                    fatal_error.set_result("pane control message is too large")
                return
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await report_error("invalid pane control message")
                continue
            if not isinstance(control, dict):
                await report_error("invalid pane control message")
                continue
            kind = control.get("type")
            if kind == "pane-output-ack":
                stream_id = control.get("stream_id")
                raw_seq = control.get("seq")
                try:
                    seq = int(raw_seq)
                except (TypeError, ValueError):
                    continue
                if isinstance(stream_id, int):
                    acknowledge_pane_frames(stream_id, seq)
            elif kind == "pane-visibility":
                stream_id = control.get("stream_id")
                visible = control.get("visible")
                if isinstance(stream_id, int) and isinstance(visible, bool):
                    window = stream_windows.get(stream_id)
                    if window is not None:
                        window.set_paused(not visible)
            elif kind == "pong":
                continue
            elif kind == "input-transfer-cancel":
                transfers.cancel()
            elif kind == "input-transfer":
                if expected_image_bytes is not None:
                    raise ValueError("clipboard image upload body is missing")
                transfer = InputTransfer(
                    control, image_limit=MAX_CLIPBOARD_IMAGE_BYTES,
                    paste_limit=MAX_PANE_TEXT_PASTE_BYTES,
                    image_extensions=IMAGE_EXTENSIONS, pane_mode=True,
                )
                transfers.start(transfer)
                if not enqueue_browser_command(transfer, transfer.size):
                    return
            else:
                if expected_image_bytes is not None:
                    if not fatal_error.done():
                        fatal_error.set_result("clipboard image upload body is missing")
                    return
                if kind == "clipboard-image":
                    if transfers.pending is not None:
                        raise ValueError("an input transfer is already in progress")
                    image_size = control.get("size")
                    if (
                        not isinstance(image_size, int)
                        or isinstance(image_size, bool)
                        or not 0 < image_size <= MAX_CLIPBOARD_IMAGE_BYTES
                    ):
                        if not fatal_error.done():
                            fatal_error.set_result("invalid clipboard image header")
                        return
                    expected_image_bytes = image_size
                if not enqueue_browser_command(control, text_size):
                    return

    async def apply_browser_commands() -> None:
        """Apply non-disposable browser commands in their received order."""
        nonlocal active_stream_id, queued_command_bytes
        pending_image: tuple[PaneStreamRequest, str, int] | None = None
        image_body: bytes | None = None
        active_command_bytes = 0
        while True:
            if image_body is not None:
                command = image_body
                image_body = None
            else:
                queued_command_bytes -= active_command_bytes
                active_command_bytes = 0
                envelope = await command_queue.get()
                if envelope is None:
                    return
                active_command_bytes = envelope.size
                command = envelope.value
                if isinstance(command, InputTransfer):
                    try:
                        result = await command.command()
                    except UnicodeDecodeError:
                        await report_error("pane paste is not valid UTF-8")
                        continue
                    if result is None:
                        continue
                    command, image_body = result
            if pending_image is not None and not isinstance(command, bytes):
                pending_image = None
                await report_error("clipboard image upload body is missing")
            if isinstance(command, bytes):
                if pending_image is not None:
                    request, extension, expected_size = pending_image
                    pending_image = None
                    if len(command) != expected_size:
                        await report_error("clipboard image upload was truncated")
                        continue
                    path: Path | None = None
                    try:
                        path = await stage_clipboard_image_async(extension, command)
                        pane_image_paths.add(path)
                        # Herdr Web and the pane use the same host. Herdr's
                        # public semantic input API applies bracketed paste.
                        await run_herdr_socket_api(
                            backend,
                            "pane.send_input",
                            {"pane_id": request.pane_id, "text": str(path)},
                        )
                        mark_interactive(request.stream_id)
                    except (OSError, RuntimeError, ValueError) as error:
                        if path is not None:
                            pane_image_paths.discard(path)
                            path.unlink(missing_ok=True)
                        await report_error(f"clipboard image rejected: {error}")
                    continue
                client = clients.get(active_stream_id)
                if (
                    not isinstance(client, PaneController)
                    or active_stream_id not in writable_streams
                ):
                    await report_error("The selected pane is read-only")
                    continue
                try:
                    await client.send_input(command)
                    mark_interactive(active_stream_id)
                except PaneStreamError as error:
                    await report_error(str(error))
                continue

            control = command
            kind = control.get("type")
            if kind == "pane-active":
                stream_id = control.get("stream_id")
                if isinstance(stream_id, int) and stream_id in clients:
                    active_stream_id = stream_id
                    mark_interactive(stream_id)
            elif kind == "pane-resize":
                stream_id = control.get("stream_id")
                client = clients.get(stream_id) if isinstance(stream_id, int) else None
                if isinstance(client, PaneController):
                    try:
                        cols = int(control["cols"])
                        rows = int(control["rows"])
                        async with stream_resize_locks[stream_id]:
                            await client.resize(cols, rows)
                            stream_sizes[stream_id] = (cols, rows)
                        mark_interactive(stream_id)
                    except (KeyError, TypeError, ValueError, PaneStreamError) as error:
                        await report_error(f"pane resize rejected: {error}")
            elif kind == "pane-scroll":
                stream_id = control.get("stream_id")
                client = clients.get(stream_id) if isinstance(stream_id, int) else None
                if isinstance(client, PaneController):
                    try:
                        await client.scroll(
                            str(control["direction"]),
                            int(control.get("lines", 1)),
                            column=(
                                int(control["column"])
                                if control.get("column") is not None
                                else None
                            ),
                            row=(
                                int(control["row"])
                                if control.get("row") is not None
                                else None
                            ),
                        )
                        mark_interactive(stream_id)
                    except (KeyError, TypeError, ValueError, PaneStreamError) as error:
                        await report_error(f"pane scroll rejected: {error}")
            elif kind == "pane-paste":
                stream_id = control.get("stream_id")
                client = clients.get(stream_id) if isinstance(stream_id, int) else None
                request = (
                    requests_by_stream.get(stream_id)
                    if isinstance(stream_id, int)
                    else None
                )
                if (
                    not isinstance(client, PaneController)
                    or modes.get(stream_id) != "control"
                    or stream_id not in writable_streams
                    or request is None
                ):
                    await report_error("The selected pane is read-only")
                    continue
                try:
                    pasted_text = validate_pane_paste_text(control.get("text"))
                    await run_herdr_socket_api(
                        backend,
                        "pane.send_input",
                        {"pane_id": request.pane_id, "text": pasted_text},
                    )
                    mark_interactive(stream_id)
                except (OSError, RuntimeError, ValueError) as error:
                    await report_error(f"pane paste rejected: {error}")
            elif kind == "pane-mouse":
                stream_id = control.get("stream_id")
                client = clients.get(stream_id) if isinstance(stream_id, int) else None
                request = (
                    requests_by_stream.get(stream_id)
                    if isinstance(stream_id, int)
                    else None
                )
                if (
                    not isinstance(client, PaneController)
                    or modes.get(stream_id) != "control"
                    or stream_id not in writable_streams
                    or request is None
                ):
                    await report_error("The selected pane is read-only")
                    continue
                try:
                    sequence = encode_pane_mouse_sequence(control)
                    await run_herdr_socket_api(
                        backend,
                        "pane.send_text",
                        {"pane_id": request.pane_id, "text": sequence},
                    )
                    mark_interactive(stream_id)
                except (OSError, RuntimeError, ValueError) as error:
                    await report_error(f"pane mouse input rejected: {error}")
            elif kind == "clipboard-image":
                stream_id = control.get("stream_id")
                client = clients.get(stream_id) if isinstance(stream_id, int) else None
                request = (
                    requests_by_stream.get(stream_id)
                    if isinstance(stream_id, int)
                    else None
                )
                extension = str(control.get("extension", "")).lower()
                size = control.get("size")
                valid_size = (
                    isinstance(size, int)
                    and not isinstance(size, bool)
                    and 0 < size <= MAX_CLIPBOARD_IMAGE_BYTES
                )
                if (
                    not isinstance(client, PaneController)
                    or modes.get(stream_id) != "control"
                    or stream_id not in writable_streams
                    or request is None
                    or extension not in IMAGE_EXTENSIONS
                    or not valid_size
                ):
                    await websocket.close(code=4400, reason="invalid clipboard image header")
                    return
                pending_image = (request, extension, size)

    async def receive_browser_input() -> None:
        try:
            await receive_browser_messages()
        except (ValueError, RuntimeError) as error:
            if not fatal_error.done():
                fatal_error.set_result(str(error))
        finally:
            transfers.cancel()

    async def run_browser_input_pipeline() -> None:
        receiver = asyncio.create_task(receive_browser_input())
        command_worker = asyncio.create_task(apply_browser_commands())
        try:
            done, _ = await asyncio.wait(
                [receiver, command_worker], return_when=asyncio.FIRST_COMPLETED
            )
            if command_worker in done:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                command_worker.result()
                return

            receiver.result()

            async def drain_commands() -> None:
                await command_queue.put(None)
                await command_worker

            # Finish already admitted input when possible, but do not retain a
            # disconnected controller for one API timeout per queued command.
            try:
                await asyncio.wait_for(
                    drain_commands(), timeout=PANE_COMMAND_DRAIN_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.info("pane command drain timed out after disconnect")
        finally:
            for task in (receiver, command_worker):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receiver, command_worker, return_exceptions=True)

    pumps = [asyncio.create_task(pump_stream(request)) for request in requests]

    async def close_output_after_pumps() -> None:
        await asyncio.gather(*pumps, return_exceptions=True)
        await queue_control(None)

    monitor = asyncio.create_task(close_output_after_pumps())
    sender = asyncio.create_task(send_browser_output())
    input_pipeline = asyncio.create_task(run_browser_input_pipeline())
    failure = asyncio.ensure_future(fatal_error)
    try:
        done, pending = await asyncio.wait(
            [sender, input_pipeline, failure],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if fatal_error.done():
            try:
                await asyncio.wait_for(
                    websocket.close(code=1011, reason=fatal_error.result()),
                    timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                )
            except (RuntimeError, asyncio.TimeoutError):
                pass
        for task in [*pumps, monitor, *pending]:
            task.cancel()
        await asyncio.gather(*pumps, monitor, *pending, return_exceptions=True)
        for task in done - {failure}:
            task.result()
    finally:
        # Cancellation of the connection handler must also cancel its input
        # placeholder, receiver and command worker before releasing clients.
        tasks = [*pumps, monitor, sender, input_pipeline, failure]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for item in acknowledgements.values():
            item.pacing_updated.set()
            item.acknowledged.set()
        await asyncio.gather(
            *(client.close() for client in tuple(all_clients)),
            return_exceptions=True,
        )
        for path in pane_image_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove staged pane image %s", path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Herdr Web browser terminal")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        default=8765,
        type=int,
        help="port to bind (default: 8765)",
    )
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    import uvicorn

    uvicorn.run(
        app,
        host=arguments.host,
        port=arguments.port,
        # Full and Pane output negotiate their own compression. A second
        # WebSocket-level pass would only burn CPU on compressed payloads.
        ws_per_message_deflate=False,
        # Herdr Web does not consume proxy identity or client-address headers.
        # Keep the transport peer authoritative and avoid accidental trust.
        proxy_headers=False,
        ws_ping_interval=WEBSOCKET_HEARTBEAT_SECONDS,
        ws_ping_timeout=WEBSOCKET_HEARTBEAT_SECONDS * 3,
    )


@app.websocket("/ws/{backend_id}")
async def terminal(websocket: WebSocket, backend_id: str) -> None:
    if not websocket_origin_is_allowed(websocket):
        await websocket.close(code=4403, reason="WebSocket origin is not allowed")
        return

    backends = discover_backends()
    backend = backends.get(backend_id)
    if backend is None:
        await websocket.close(code=4404, reason="Herdr backend is no longer available")
        return

    await websocket.accept()
    client: PtyClient | None = None
    try:
        # The UI sends this before attaching, but retain sane defaults for
        # callers that use the WebSocket endpoint directly.
        initial = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if initial.get("type") == "panes.attach":
            await run_panes_websocket(websocket, backend, initial)
            return
        if initial.get("type") != "resize":
            await websocket.close(code=4400, reason="expected initial resize or pane attachment")
            return
        cols = int(initial.get("cols", 120))
        rows = int(initial.get("rows", 40))
        output_ack_enabled = initial.get("output_ack") is True
        output_encoder = (
            FullOutputEncoder()
            if output_ack_enabled
            and initial.get("output_compression") == FULL_OUTPUT_COMPRESSION
            else None
        )
        client = start_client(backend, cols, rows)
        await asyncio.wait_for(
            websocket.send_json(
                {
                    "type": "attached",
                    "input_chunk_bytes": INPUT_TRANSFER_CHUNK_BYTES,
                    "label": backend.label,
                    "output_compression": (
                        FULL_OUTPUT_COMPRESSION if output_encoder is not None else None
                    ),
                    "output_window_bytes": (
                        OUTPUT_ACK_WINDOW_BYTES if output_ack_enabled else 0
                    ),
                }
            ),
            timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
        )

        # Keep only one PTY chunk between the client and WebSocket sender.
        # Early backpressure lets Herdr coalesce slow-client frames at its
        # semantic render layer instead of building a stale ANSI burst here.
        output: asyncio.Queue[bytes | dict[str, str] | None] = asyncio.Queue(
            maxsize=OUTPUT_QUEUE_SIZE
        )
        output_window = FullOutputWindow()
        output_acknowledged = asyncio.Event()
        pending_output_error: dict[str, str] | None = None

        async def read_pty_output() -> None:
            while True:
                try:
                    data = await read_pty_chunk(client.master_fd)
                except OSError:
                    break
                if not data:
                    break
                await output.put(data)
            await output.put(None)

        def output_ack_timeout() -> float | None:
            if not output_ack_enabled:
                return None
            remaining = output_window.seconds_until_timeout(
                time.monotonic(), OUTPUT_ACK_TIMEOUT_SECONDS
            )
            if remaining is not None and remaining <= 0:
                raise RuntimeError("terminal parser acknowledgement timed out")
            return remaining

        async def wait_for_output_ack() -> None:
            output_acknowledged.clear()
            try:
                await asyncio.wait_for(
                    output_acknowledged.wait(), timeout=output_ack_timeout()
                )
            except asyncio.TimeoutError as error:
                raise RuntimeError(
                    "terminal parser acknowledgement timed out"
                ) from error

        async def send_output_chunk(chunk: bytes) -> None:
            if output_encoder is None:
                await websocket.send_bytes(chunk)
                return
            packet = output_encoder.encode(chunk)
            # This sender is the only output writer. Do not put an awaitable
            # producer or heartbeat between this descriptor/payload pair.
            if packet.descriptor is not None:
                await websocket.send_json(packet.descriptor)
            await websocket.send_bytes(packet.payload)

        async def send_browser_output() -> None:
            nonlocal pending_output_error
            while True:
                if pending_output_error is not None:
                    item = pending_output_error
                    pending_output_error = None
                else:
                    try:
                        ack_timeout = output_ack_timeout()
                        item = await asyncio.wait_for(
                            output.get(),
                            timeout=min(WEBSOCKET_HEARTBEAT_SECONDS, ack_timeout)
                            if ack_timeout is not None
                            else WEBSOCKET_HEARTBEAT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        output_ack_timeout()
                        await asyncio.wait_for(
                            websocket.send_json({"type": "ping"}),
                            timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                        )
                        continue
                if item is None:
                    while output_ack_enabled and output_window.inflight_bytes:
                        await wait_for_output_ack()
                    return
                if isinstance(item, bytes):
                    for chunk in websocket_output_chunks(item):
                        while output_ack_enabled and not output_window.has_room(len(chunk)):
                            await wait_for_output_ack()
                        output_ack_timeout()
                        if output_ack_enabled:
                            # Reserve raw bytes before the await: a fast parser
                            # ACK must not race ahead of the sent counter.
                            output_window.note_sent(len(chunk), time.monotonic())
                        try:
                            await asyncio.wait_for(
                                send_output_chunk(chunk),
                                timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError as error:
                            raise RuntimeError("terminal WebSocket send timed out") from error
                else:
                    await asyncio.wait_for(
                        websocket.send_json(item), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS
                    )

        async def report_error(message: str) -> None:
            nonlocal pending_output_error
            error = {"type": "error", "message": message}
            try:
                output.put_nowait(error)
            except asyncio.QueueFull:
                pending_output_error = error

        command_queue: asyncio.Queue[PaneBrowserCommand | None] = asyncio.Queue(
            maxsize=PANE_COMMAND_QUEUE_SIZE
        )
        queued_command_bytes = 0
        transfers = InputTransferReceiver()

        def enqueue_browser_command(
            command: bytes | dict[str, object] | InputTransfer, size: int
        ) -> None:
            nonlocal queued_command_bytes
            if queued_command_bytes + size > PANE_COMMAND_QUEUE_BYTES:
                raise RuntimeError("terminal command queue is full")
            try:
                command_queue.put_nowait(PaneBrowserCommand(command, size))
            except asyncio.QueueFull as error:
                raise RuntimeError("terminal command queue is full") from error
            queued_command_bytes += size

        async def receive_browser_messages() -> None:
            expected_image_bytes: int | None = None
            try:
                while True:
                    message = await transfers.receive(websocket)
                    if message["type"] == "websocket.disconnect":
                        logger.info(
                            "websocket client disconnected backend=%s code=%s",
                            backend.label, message.get("code"),
                        )
                        return
                    data = message.get("bytes")
                    if data is not None:
                        if transfers.feed(data):
                            continue
                        limit = (MAX_CLIPBOARD_IMAGE_BYTES if expected_image_bytes is not None
                                 else PANE_INPUT_MESSAGE_BYTES)
                        if len(data) > limit:
                            raise RuntimeError("terminal input message is too large")
                        if expected_image_bytes is not None and len(data) != expected_image_bytes:
                            raise RuntimeError("clipboard image upload was truncated")
                        expected_image_bytes = None
                        enqueue_browser_command(data, len(data))
                        continue
                    text = message.get("text")
                    if text is None:
                        continue
                    text_size = len(text.encode("utf-8"))
                    if text_size > PANE_CONTROL_MESSAGE_BYTES:
                        raise RuntimeError("terminal control message is too large")
                    try:
                        control = json.loads(text)
                    except json.JSONDecodeError:
                        await report_error("invalid terminal control message")
                        continue
                    if not isinstance(control, dict):
                        await report_error("invalid terminal control message")
                        continue
                    kind = control.get("type")
                    if kind == "output-ack":
                        if output_ack_enabled and output_window.acknowledge(
                            control.get("bytes"), time.monotonic()
                        ):
                            output_acknowledged.set()
                    elif kind == "pong":
                        continue
                    elif kind == "input-transfer-cancel":
                        transfers.cancel()
                    elif kind == "input-transfer":
                        if expected_image_bytes is not None:
                            raise RuntimeError("clipboard image upload body is missing")
                        try:
                            transfer = InputTransfer(
                                control, image_limit=MAX_CLIPBOARD_IMAGE_BYTES,
                                paste_limit=MAX_PANE_TEXT_PASTE_BYTES,
                                image_extensions=IMAGE_EXTENSIONS, pane_mode=False,
                            )
                            transfers.start(transfer)
                        except ValueError as error:
                            raise RuntimeError(str(error)) from error
                        enqueue_browser_command(transfer, transfer.size)
                    else:
                        if expected_image_bytes is not None:
                            raise RuntimeError("clipboard image upload body is missing")
                        if kind == "clipboard-image":
                            if transfers.pending is not None:
                                raise RuntimeError("an input transfer is already in progress")
                            size = control.get("size")
                            if (not isinstance(size, int) or isinstance(size, bool)
                                    or not 0 < size <= MAX_CLIPBOARD_IMAGE_BYTES):
                                raise RuntimeError("invalid clipboard image header")
                            expected_image_bytes = size
                        enqueue_browser_command(control, text_size)
            except ValueError as error:
                raise RuntimeError(str(error)) from error
            finally:
                transfers.cancel()

        async def apply_browser_commands() -> None:
            nonlocal queued_command_bytes
            pending_image: tuple[str, int] | None = None
            image_body: bytes | None = None
            active_command_bytes = 0
            while True:
                if image_body is not None:
                    command = image_body
                    image_body = None
                else:
                    queued_command_bytes -= active_command_bytes
                    active_command_bytes = 0
                    envelope = await command_queue.get()
                    if envelope is None:
                        return
                    active_command_bytes = envelope.size
                    command = envelope.value
                    if isinstance(command, InputTransfer):
                        result = await command.command()
                        if result is None:
                            continue
                        command, image_body = result
                if isinstance(command, bytes):
                    if pending_image is None:
                        try:
                            await write_pty(client.master_fd, command)
                        except OSError:
                            raise RuntimeError("terminal session is closed")
                        continue
                    extension, expected_size = pending_image
                    pending_image = None
                    if len(command) != expected_size:
                        raise RuntimeError("clipboard image upload was truncated")
                    path: Path | None = None
                    try:
                        path = await stage_clipboard_image_async(extension, command)
                        await write_pty(
                            client.master_fd,
                            b"\x1b[200~" + os.fsencode(path) + b"\x1b[201~",
                        )
                    except (OSError, ValueError) as error:
                        await report_error(f"clipboard image rejected: {error}")
                    finally:
                        if path is not None:
                            schedule_staged_image_removal(path)
                    continue
                if command.get("type") == "resize":
                    try:
                        client.resize(int(command["cols"]), int(command["rows"]))
                    except (KeyError, TypeError, ValueError):
                        await report_error("invalid terminal resize")
                elif command.get("type") == "clipboard-image":
                    extension = str(command.get("extension", "")).lower()
                    # Even an invalid extension consumes its declared body; it
                    # must never fall through as raw terminal input.
                    pending_image = (extension, command["size"])
                    if extension not in IMAGE_EXTENSIONS:
                        await report_error("invalid clipboard image header")

        async def browser_to_pty() -> None:
            receiver = asyncio.create_task(receive_browser_messages())
            worker = asyncio.create_task(apply_browser_commands())
            try:
                done, _ = await asyncio.wait(
                    [receiver, worker], return_when=asyncio.FIRST_COMPLETED
                )
                if worker in done:
                    worker.result()
                    return
                receiver.result()

                async def drain_commands() -> None:
                    await command_queue.put(None)
                    await worker

                try:
                    await asyncio.wait_for(
                        drain_commands(), timeout=PANE_COMMAND_DRAIN_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    logger.info("terminal command drain timed out after disconnect")
            finally:
                for task in (receiver, worker):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(receiver, worker, return_exceptions=True)

        reader = asyncio.create_task(read_pty_output())
        sender = asyncio.create_task(send_browser_output())
        input_reader = asyncio.create_task(browser_to_pty())
        try:
            done, _ = await asyncio.wait(
                [sender, input_reader],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        finally:
            for task in (reader, sender, input_reader):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, sender, input_reader, return_exceptions=True)
    except WebSocketDisconnect as error:
        logger.info("websocket disconnected backend=%s code=%s", backend.label, error.code)
    except asyncio.TimeoutError:
        logger.info("terminal websocket operation timed out backend=%s", backend.label)
    except RuntimeError as error:
        try:
            await asyncio.wait_for(
                websocket.send_json({"type": "error", "message": str(error)}),
                timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
            logger.info("could not report terminal websocket error backend=%s", backend.label)
        try:
            await asyncio.wait_for(
                websocket.close(code=1011, reason=str(error)[:120]),
                timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
            pass
    finally:
        if client is not None:
            await client.close()


if __name__ == "__main__":
    main()
