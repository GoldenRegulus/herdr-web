"""A local, Jupyter-proxy-friendly browser terminal for Herdr sessions."""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import os
from pathlib import Path
import pty
import shutil
import signal
import socket
import struct
import termios
import tempfile
import secrets
from dataclasses import dataclass, field
from typing import Final

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles

PACKAGE_DIR: Final = Path(__file__).resolve().parent
STATIC_DIR: Final = PACKAGE_DIR / "static"
MAX_TERMINAL_DIMENSION: Final = 500
MAX_CLIPBOARD_IMAGE_BYTES: Final = 16 * 1024 * 1024
DISPLAY_FRAME_INTERVAL: Final = 1 / 60
IMAGE_EXTENSIONS: Final = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})

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

    def resize(self, cols: int, rows: int) -> None:
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

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass


@dataclass
class BrowserSession:
    client: PtyClient
    output: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    reader: asyncio.Task[None] | None = None
    closed: bool = False


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


def write_pty(master_fd: int, data: bytes) -> None:
    """Preserve complete key/paste payloads when a PTY performs a short write."""
    remaining = memoryview(data)
    while remaining:
        written = os.write(master_fd, remaining)
        if written <= 0:
            raise OSError("PTY write made no progress")
        remaining = remaining[written:]


def stage_clipboard_image(extension: str, data: bytes) -> Path:
    if extension not in IMAGE_EXTENSIONS:
        raise ValueError("unsupported clipboard image format")
    if not data or len(data) > MAX_CLIPBOARD_IMAGE_BYTES:
        raise ValueError("clipboard image exceeds Herdr's 16 MiB limit")
    directory = Path(tempfile.gettempdir()) / "herdr-web-images"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="image-", suffix=f".{extension}", dir=directory)
    path = Path(raw_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as image:
            image.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


async def remove_staged_image_later(path: Path) -> None:
    # The client synchronously reads the path after its bracketed-paste event;
    # retain it briefly for that handoff, then remove this bridge-owned file.
    await asyncio.sleep(30)
    path.unlink(missing_ok=True)


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
        # Herdr maps its terminal-toast delivery to OSC 9 for this backend.
        # The browser's xterm parser consumes OSC 9 and displays a web toast.
        environment["TERM_PROGRAM"] = "ghostty"
        # Make the normal Herdr client write clipboard updates as OSC 52 to
        # this PTY, where the browser can apply them to its real clipboard.
        environment["SSH_TTY"] = "herdr-web"
        # Reuse Herdr's existing Unix remote-client file-drop path for browser
        # clipboard images. The web bridge stages the file and pastes its path.
        environment["HERDR_REMOTE_KEYBINDINGS"] = "server"
        os.execvpe(binary, [binary, "client"], environment)

    client = PtyClient(pid, master_fd)
    client.resize(cols, rows)
    return client


@app.get("/")
async def index(request: Request) -> HTMLResponse:
    # jupyter-server-proxy removes /proxy/<port> before forwarding, but passes
    # it here so generated browser asset/API URLs retain the required prefix.
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    base_path = f"{prefix}/" if prefix else "/"
    document = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(document.replace("{{HERDR_WEB_BASE_PATH}}", base_path))


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
                data = await asyncio.to_thread(os.read, session.client.master_fd, 16 * 1024)
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
    session.client.close()


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
    try:
        data = base64.b64decode(request.data_base64, validate=True)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="input is not valid base64") from error
    try:
        write_pty(session.client.master_fd, data)
    except OSError as error:
        raise HTTPException(status_code=410, detail="terminal session is closed") from error
    return {"ok": True}


@app.post("/api/sessions/{session_id}/resize")
async def resize_session(session_id: str, request: TerminalResize) -> dict[str, bool]:
    session_or_404(session_id).client.resize(request.cols, request.rows)
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

    uvicorn.run(app, host=arguments.host, port=arguments.port)


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
        client = start_client(backend, cols, rows)
        await websocket.send_json({"type": "attached", "label": backend.label})

        # Bound queued chunks so a slow browser applies backpressure instead
        # of allowing an unbounded terminal-output allocation.
        output: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=120)

        async def read_pty() -> None:
            while True:
                try:
                    data = await asyncio.to_thread(os.read, client.master_fd, 16 * 1024)
                except OSError:
                    break
                if not data:
                    break
                await output.put(data)
            await output.put(None)

        async def send_changed_frames() -> None:
            # Herdr's TerminalAnsi render stream already has stable frame
            # identity: unchanged FrameData is suppressed, and changed frames
            # are ANSI diffs. Do not byte-deduplicate here: equal ANSI chunks
            # can still be meaningful incremental terminal operations.
            # Instead, batch only changed bytes into one transport event per
            # display interval, avoiding network/UI churn above 60 Hz.
            while True:
                first = await output.get()
                if first is None:
                    return
                chunks = [first]
                await asyncio.sleep(DISPLAY_FRAME_INTERVAL)
                closed = False
                while True:
                    try:
                        item = output.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is None:
                        closed = True
                        break
                    chunks.append(item)
                await websocket.send_bytes(b"".join(chunks))
                if closed:
                    return

        async def browser_to_pty() -> None:
            pending_image: tuple[str, int] | None = None
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    if pending_image is None:
                        write_pty(client.master_fd, data)
                        continue
                    extension, expected_size = pending_image
                    pending_image = None
                    if len(data) != expected_size:
                        await websocket.send_json(
                            {"type": "error", "message": "clipboard image upload was truncated"}
                        )
                        continue
                    try:
                        path = stage_clipboard_image(extension, data)
                        # Herdr's remote client recognizes an absolute image
                        # path inside bracketed paste and emits ClipboardImage.
                        write_pty(
                            client.master_fd,
                            b"\x1b[200~" + os.fsencode(path) + b"\x1b[201~",
                        )
                        asyncio.create_task(remove_staged_image_later(path))
                    except (OSError, ValueError) as error:
                        await websocket.send_json(
                            {"type": "error", "message": f"clipboard image rejected: {error}"}
                        )
                    continue
                text = message.get("text")
                if text is None:
                    continue
                # Only JSON control messages are sent as text. All terminal
                # input is binary, so paste and escape sequences stay exact.
                import json

                control = json.loads(text)
                if control.get("type") == "resize":
                    client.resize(int(control["cols"]), int(control["rows"]))
                elif control.get("type") == "clipboard-image":
                    extension = str(control.get("extension", "")).lower()
                    size = control.get("size")
                    if extension not in IMAGE_EXTENSIONS or not isinstance(size, int):
                        await websocket.send_json(
                            {"type": "error", "message": "invalid clipboard image header"}
                        )
                    elif size <= 0 or size > MAX_CLIPBOARD_IMAGE_BYTES:
                        await websocket.send_json(
                            {"type": "error", "message": "clipboard image exceeds Herdr's 16 MiB limit"}
                        )
                    else:
                        pending_image = (extension, size)

        reader = asyncio.create_task(read_pty())
        sender = asyncio.create_task(send_changed_frames())
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
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except RuntimeError as error:
        await websocket.send_json({"type": "error", "message": str(error)})
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
