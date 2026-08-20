"""A local, Jupyter-proxy-friendly browser terminal for Herdr sessions."""

from __future__ import annotations

import argparse
import asyncio
import base64
import errno
import fcntl
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
from dataclasses import dataclass, field
from typing import Final

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PACKAGE_DIR: Final = Path(__file__).resolve().parent
STATIC_DIR: Final = PACKAGE_DIR / "static"
STAGED_IMAGE_DIRECTORY: Final = Path(tempfile.gettempdir()) / "herdr-web-images"
MAX_TERMINAL_DIMENSION: Final = 500
MAX_CLIPBOARD_IMAGE_BYTES: Final = 16 * 1024 * 1024
PTY_READ_SIZE: Final = 256 * 1024
PTY_COALESCE_SECONDS: Final = 0.002
OUTPUT_QUEUE_SIZE: Final = 1
OUTPUT_ACK_WINDOW_BYTES: Final = 128 * 1024
WEBSOCKET_HEARTBEAT_SECONDS: Final = 15
HTTP_SESSION_IDLE_SECONDS: Final = 30
CHILD_TERMINATION_GRACE_SECONDS: Final = 1
CHILD_KILL_GRACE_SECONDS: Final = 1
IMAGE_EXTENSIONS: Final = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
logger = logging.getLogger(__name__)

app = FastAPI(title="herdr-web", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


class TerminalInput(BaseModel):
    data_base64: str


class TerminalResize(BaseModel):
    cols: int
    rows: int


sessions: dict[str, BrowserSession] = {}
session_reaper: asyncio.Task[None] | None = None
staged_image_cleanup_tasks: set[asyncio.Task[None]] = set()


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


def stage_clipboard_image(extension: str, data: bytes) -> Path:
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
            image.write(data)
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


def start_client(backend: Backend, cols: int, rows: int) -> PtyClient:
    """Run Herdr's existing terminal client in a PTY.

    This intentionally does not reimplement Herdr's private bincode protocol.
    The normal client receives TerminalAnsi frames from the selected client
    socket, while the browser supplies the terminal that client expects.
    """
    binary = herdr_binary()
    pid, master_fd = pty.fork()
    if pid == 0:
        environment = os.environ.copy()
        # The bridge can run inside Herdr. Do not make its child client look
        # like a nested launch or bind it to the parent's pane.
        for variable in (
            "HERDR_ENV",
            "HERDR_SOCKET_PATH",
            "HERDR_SESSION",
            "HERDR_TAB_ID",
            "HERDR_WORKSPACE_ID",
            "HERDR_PANE_ID",
        ):
            environment.pop(variable, None)
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
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/backends")
async def list_backends() -> dict[str, list[dict[str, str]]]:
    return {
        "backends": [
            {"id": backend.id, "label": backend.label}
            for backend in discover_backends().values()
        ]
    }


async def read_pty(session: BrowserSession) -> None:
    try:
        while True:
            try:
                data = await read_pty_chunk(session.client.master_fd)
            except OSError:
                return
            if not data:
                return
            await session.output.put(data)
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
    while sum(map(len, chunks)) < 64 * 1024:
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
        ws_ping_interval=WEBSOCKET_HEARTBEAT_SECONDS,
        ws_ping_timeout=WEBSOCKET_HEARTBEAT_SECONDS * 3,
    )


@app.websocket("/ws/{backend_id}")
async def terminal(websocket: WebSocket, backend_id: str) -> None:
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
        if initial.get("type") != "resize":
            await websocket.close(code=4400, reason="expected initial resize")
            return
        cols = int(initial.get("cols", 120))
        rows = int(initial.get("rows", 40))
        output_ack_enabled = initial.get("output_ack") is True
        client = start_client(backend, cols, rows)
        await websocket.send_json(
            {
                "type": "attached",
                "label": backend.label,
                "output_window_bytes": (
                    OUTPUT_ACK_WINDOW_BYTES if output_ack_enabled else 0
                ),
            }
        )

        # Keep only one PTY chunk between the client and WebSocket sender.
        # Early backpressure lets Herdr coalesce slow-client frames at its
        # semantic render layer instead of building a stale ANSI burst here.
        output: asyncio.Queue[bytes | dict[str, str] | None] = asyncio.Queue(
            maxsize=OUTPUT_QUEUE_SIZE
        )
        output_bytes_sent = 0
        output_bytes_acknowledged = 0
        output_acknowledged = asyncio.Event()

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

        async def send_browser_output() -> None:
            nonlocal output_bytes_sent
            while True:
                try:
                    item = await asyncio.wait_for(
                        output.get(), timeout=WEBSOCKET_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue
                if item is None:
                    return
                if isinstance(item, bytes):
                    while (
                        output_ack_enabled
                        and output_bytes_sent - output_bytes_acknowledged
                        >= OUTPUT_ACK_WINDOW_BYTES
                    ):
                        output_acknowledged.clear()
                        if (
                            output_bytes_sent - output_bytes_acknowledged
                            >= OUTPUT_ACK_WINDOW_BYTES
                        ):
                            await output_acknowledged.wait()
                    # Increment before the await so a fast browser ACK cannot
                    # race ahead of this task's cumulative byte counter.
                    output_bytes_sent += len(item)
                    await websocket.send_bytes(item)
                else:
                    await websocket.send_json(item)

        async def report_error(message: str) -> None:
            await output.put({"type": "error", "message": message})

        async def browser_to_pty() -> None:
            nonlocal output_bytes_acknowledged
            pending_image: tuple[str, int] | None = None
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    logger.info(
                        "websocket client disconnected backend=%s code=%s",
                        backend.label,
                        message.get("code"),
                    )
                    return
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    if pending_image is None:
                        try:
                            await write_pty(client.master_fd, data)
                        except OSError:
                            await report_error("terminal session is closed")
                            return
                        continue
                    extension, expected_size = pending_image
                    pending_image = None
                    if len(data) != expected_size:
                        await report_error("clipboard image upload was truncated")
                        continue
                    path: Path | None = None
                    try:
                        path = stage_clipboard_image(extension, data)
                        # Herdr's remote client recognizes an absolute image
                        # path inside bracketed paste and emits ClipboardImage.
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
                text = message.get("text")
                if text is None:
                    continue
                # Only JSON control messages are sent as text. All terminal
                # input is binary, so paste and escape sequences stay exact.
                control = json.loads(text)
                if control.get("type") == "resize":
                    client.resize(int(control["cols"]), int(control["rows"]))
                elif control.get("type") == "output-ack":
                    acknowledged = control.get("bytes")
                    if (
                        isinstance(acknowledged, int)
                        and output_bytes_acknowledged < acknowledged <= output_bytes_sent
                    ):
                        output_bytes_acknowledged = acknowledged
                        output_acknowledged.set()
                elif control.get("type") == "clipboard-image":
                    extension = str(control.get("extension", "")).lower()
                    size = control.get("size")
                    if extension not in IMAGE_EXTENSIONS or not isinstance(size, int):
                        await report_error("invalid clipboard image header")
                    elif size <= 0 or size > MAX_CLIPBOARD_IMAGE_BYTES:
                        await report_error("clipboard image exceeds Herdr's 16 MiB limit")
                    else:
                        pending_image = (extension, size)
                elif control.get("type") == "pong":
                    continue

        reader = asyncio.create_task(read_pty_output())
        sender = asyncio.create_task(send_browser_output())
        input_reader = asyncio.create_task(browser_to_pty())
        done, pending = await asyncio.wait(
            [sender, input_reader],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in [reader, *pending]:
            task.cancel()
        await asyncio.gather(reader, *pending, return_exceptions=True)
        for task in done:
            task.result()
    except WebSocketDisconnect as error:
        logger.info("websocket disconnected backend=%s code=%s", backend.label, error.code)
    except asyncio.TimeoutError:
        logger.info("websocket initial resize timed out backend=%s", backend.label)
    except RuntimeError as error:
        await websocket.send_json({"type": "error", "message": str(error)})
    finally:
        if client is not None:
            await client.close()


if __name__ == "__main__":
    main()
