"""A local, Jupyter-proxy-friendly browser terminal for Herdr sessions."""

from __future__ import annotations

import argparse
import asyncio
import atexit
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
from typing import Final, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
PTY_READ_SIZE: Final = 256 * 1024
PTY_COALESCE_SECONDS: Final = 0.002
OUTPUT_QUEUE_SIZE: Final = 1
OUTPUT_ACK_WINDOW_BYTES: Final = 128 * 1024
WEBSOCKET_HEARTBEAT_SECONDS: Final = 15
PANE_FRAME_ACK_TIMEOUT_SECONDS: Final = 60
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
PANE_FRAME_MAGIC: Final = b"HWP1"
PANE_FRAME_HEADER: Final = struct.Struct("!4sIQBHH")
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


@dataclass(frozen=True)
class PaneStreamRequest:
    stream_id: int
    pane_id: str
    cols: int
    rows: int


@dataclass
class PaneOutputFrame:
    stream_id: int
    frame: AnsiFrame
    acknowledged: asyncio.Event


sessions: dict[str, BrowserSession] = {}
session_reaper: asyncio.Task[None] | None = None
staged_image_cleanup_tasks: set[asyncio.Task[None]] = set()
named_session_start_lock = asyncio.Lock()
navigation_snapshot_cache: dict[str, tuple[float, dict[str, object]]] = {}


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


def project_navigation_snapshot(document: dict[str, object]) -> dict[str, object]:
    """Return only the structured state needed by the browser navigation UI."""
    result = document.get("result")
    snapshot = result.get("snapshot") if isinstance(result, dict) else None
    if not isinstance(snapshot, dict):
        raise RuntimeError("Herdr API response does not contain a snapshot")

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


@app.get("/api/backends")
async def list_backends() -> dict[str, list[dict[str, str]]]:
    return {
        "backends": [
            {"id": backend.id, "label": backend.label}
            for backend in discover_backends().values()
        ]
    }


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

    requests_by_stream = {request.stream_id: request for request in requests}
    clients: dict[int, PaneStream] = {}
    modes: dict[int, str] = {}
    writable_streams: set[int] = set()
    all_clients: list[PaneStream] = []
    try:
        for request in requests:
            client = await start_pane_stream(backend, request, control=True)
            clients[request.stream_id] = client
            modes[request.stream_id] = "control"
            all_clients.append(client)
    except (OSError, RuntimeError) as error:
        await asyncio.gather(*(client.close() for client in all_clients))
        await websocket.send_json({"type": "error", "message": str(error)})
        return

    await websocket.send_json(
        {
            "type": "panes-attached",
            "tab_id": tab_id,
            "streams": [
                {
                    "stream_id": request.stream_id,
                    "pane_id": request.pane_id,
                    "mode": "control",
                }
                for request in requests
            ],
        }
    )

    output: asyncio.Queue[PaneOutputFrame | dict[str, object] | None] = asyncio.Queue(
        maxsize=OUTPUT_QUEUE_SIZE
    )
    acknowledgements: dict[tuple[int, int], asyncio.Event] = {}
    fatal_error: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    active_stream_id = requests[0].stream_id
    pane_image_paths: set[Path] = set()

    async def pump_stream(request: PaneStreamRequest) -> None:
        stream_id = request.stream_id
        client = clients[stream_id]
        received_frame = False
        while True:
            try:
                record = await client.read_record()
            except PaneStreamError as error:
                await output.put(
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
                    try:
                        client = await start_pane_stream(backend, request, control=False)
                    except (OSError, RuntimeError) as error:
                        await output.put(
                            {
                                "type": "pane-closed",
                                "stream_id": stream_id,
                                "reason": str(error),
                            }
                        )
                        return
                    clients[stream_id] = client
                    all_clients.append(client)
                    modes[stream_id] = "observe"
                    await output.put(
                        {"type": "pane-mode", "stream_id": stream_id, "mode": "observe"}
                    )
                    received_frame = False
                    continue
                await output.put(
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
            acknowledged = asyncio.Event()
            key = (stream_id, record.seq)
            acknowledgements[key] = acknowledged
            try:
                await output.put(PaneOutputFrame(stream_id, record, acknowledged))
                try:
                    await asyncio.wait_for(
                        acknowledged.wait(), timeout=PANE_FRAME_ACK_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    if not fatal_error.done():
                        fatal_error.set_result("pane parser acknowledgement timed out")
                    return
            finally:
                acknowledgements.pop(key, None)

    async def send_browser_output() -> None:
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
            if isinstance(item, PaneOutputFrame):
                frame = item.frame
                header = PANE_FRAME_HEADER.pack(
                    PANE_FRAME_MAGIC,
                    item.stream_id,
                    frame.seq,
                    int(frame.full),
                    frame.width,
                    frame.height,
                )
                await websocket.send_bytes(header + frame.bytes)
            else:
                await websocket.send_json(item)

    async def report_error(message: str) -> None:
        await output.put({"type": "error", "message": message})

    async def browser_to_panes() -> None:
        nonlocal active_stream_id
        pending_image: tuple[PaneStreamRequest, str, int] | None = None
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                if pending_image is not None:
                    request, extension, expected_size = pending_image
                    pending_image = None
                    if len(data) != expected_size:
                        await report_error("clipboard image upload was truncated")
                        continue
                    path: Path | None = None
                    try:
                        path = stage_clipboard_image(extension, data)
                        pane_image_paths.add(path)
                        # The pane and Herdr Web run on the same host. Paste the
                        # staged absolute path through Herdr's public semantic
                        # text-input API. Herdr applies the target runtime's
                        # bracketed-paste mode, which matches its ClipboardImage
                        # path handoff without using the private client protocol.
                        await run_herdr_socket_api(
                            backend,
                            "pane.send_input",
                            {"pane_id": request.pane_id, "text": str(path)},
                        )
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
                    await client.send_input(data)
                except PaneStreamError as error:
                    await report_error(str(error))
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                await report_error("invalid pane control message")
                continue
            if not isinstance(control, dict):
                await report_error("invalid pane control message")
                continue
            kind = control.get("type")
            if kind == "pane-active":
                stream_id = control.get("stream_id")
                if isinstance(stream_id, int) and stream_id in clients:
                    active_stream_id = stream_id
            elif kind == "pane-resize":
                stream_id = control.get("stream_id")
                client = clients.get(stream_id) if isinstance(stream_id, int) else None
                if isinstance(client, PaneController):
                    try:
                        await client.resize(int(control["cols"]), int(control["rows"]))
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
                except (OSError, RuntimeError, ValueError) as error:
                    await report_error(f"pane mouse input rejected: {error}")
            elif kind == "pane-output-ack":
                stream_id = control.get("stream_id")
                raw_seq = control.get("seq")
                try:
                    seq = int(raw_seq)
                except (TypeError, ValueError):
                    continue
                if isinstance(stream_id, int):
                    acknowledged = acknowledgements.get((stream_id, seq))
                    if acknowledged is not None:
                        acknowledged.set()
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
            elif kind == "pong":
                continue

    pumps = [asyncio.create_task(pump_stream(request)) for request in requests]

    async def close_output_after_pumps() -> None:
        await asyncio.gather(*pumps, return_exceptions=True)
        await output.put(None)

    monitor = asyncio.create_task(close_output_after_pumps())
    sender = asyncio.create_task(send_browser_output())
    receiver = asyncio.create_task(browser_to_panes())
    failure = asyncio.ensure_future(fatal_error)
    try:
        done, pending = await asyncio.wait(
            [sender, receiver, failure], return_when=asyncio.FIRST_COMPLETED
        )
        if failure in done:
            try:
                await websocket.close(code=1011, reason=failure.result())
            except RuntimeError:
                pass
        for task in [*pumps, monitor, *pending]:
            task.cancel()
        await asyncio.gather(*pumps, monitor, *pending, return_exceptions=True)
        for task in done - {failure}:
            task.result()
    finally:
        for acknowledged in acknowledgements.values():
            acknowledged.set()
        await asyncio.gather(
            *(client.close() for client in dict.fromkeys(all_clients)),
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
