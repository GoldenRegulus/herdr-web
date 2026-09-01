"""Async adapter for Herdr public terminal-session NDJSON streams."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
import json
import logging
import os
import signal
from typing import Final

MAX_TERMINAL_DIMENSION: Final = 500
MAX_FRAME_BYTES: Final = 2 * 1024 * 1024
# A base64 representation of a 2 MiB frame is about 2.7 MiB. Leave room for
# the other record fields while keeping one untrusted line finite.
MAX_NDJSON_LINE_BYTES: Final = 3 * 1024 * 1024
STDERR_TAIL_BYTES: Final = 16 * 1024
COMMAND_DRAIN_TIMEOUT_SECONDS: Final = 1.0
RELEASE_GRACE_SECONDS: Final = 1.0
TERMINATE_GRACE_SECONDS: Final = 1.0
KILL_GRACE_SECONDS: Final = 1.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnsiFrame:
    """One ANSI terminal frame from Herdr."""

    seq: int
    width: int
    height: int
    full: bool
    bytes: bytes


@dataclass(frozen=True)
class TerminalClosed:
    """Herdr's terminal-closed notification."""

    reason: str


class PaneStreamError(RuntimeError):
    """Base error for a terminal-session stream."""


class PaneStreamProtocolError(PaneStreamError):
    """Herdr sent a record that does not match the public stream protocol."""


class PaneStreamClosedError(PaneStreamError):
    """The local terminal-session stream cannot accept another command."""


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _terminal_dimension(value: object, name: str) -> int:
    if not _is_integer(value) or not 1 <= value <= MAX_TERMINAL_DIMENSION:
        raise ValueError(f"{name} must be an integer from 1 to {MAX_TERMINAL_DIMENSION}")
    return value


def _protocol_error(message: str) -> PaneStreamProtocolError:
    return PaneStreamProtocolError(f"invalid terminal-session record: {message}")


def _parse_record(line: bytes, previous_seq: int | None) -> AnsiFrame | TerminalClosed:
    try:
        value = line.decode("utf-8")
        record = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _protocol_error("record is not valid JSON") from error
    if not isinstance(record, dict):
        raise _protocol_error("record must be a JSON object")

    kind = record.get("type")
    if kind == "terminal.closed":
        reason = record.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise _protocol_error("terminal.closed reason must be a string or null")
        return TerminalClosed(reason or "terminal stream closed")
    if kind != "terminal.frame":
        raise _protocol_error("record type is not supported")

    if record.get("encoding") != "ansi":
        raise _protocol_error("terminal.frame encoding must be ansi")
    seq = record.get("seq")
    if not _is_integer(seq) or not 0 <= seq <= 2**64 - 1:
        raise _protocol_error("terminal.frame seq is invalid")
    if previous_seq is not None and seq <= previous_seq:
        raise _protocol_error("terminal.frame seq is not increasing")
    width = record.get("width")
    height = record.get("height")
    if not _is_integer(width) or not 1 <= width <= MAX_TERMINAL_DIMENSION:
        raise _protocol_error("terminal.frame width is invalid")
    if not _is_integer(height) or not 1 <= height <= MAX_TERMINAL_DIMENSION:
        raise _protocol_error("terminal.frame height is invalid")
    full = record.get("full")
    if not isinstance(full, bool):
        raise _protocol_error("terminal.frame full must be a boolean")
    encoded = record.get("bytes")
    if not isinstance(encoded, str):
        raise _protocol_error("terminal.frame bytes must be a base64 string")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise _protocol_error("terminal.frame bytes are not valid base64") from error
    if len(data) > MAX_FRAME_BYTES:
        raise _protocol_error("terminal.frame exceeds 2 MiB")
    return AnsiFrame(seq, width, height, full, data)


class PaneStream(AsyncIterator[AnsiFrame | TerminalClosed]):
    """A running public terminal-session stream.

    Use :func:`open_pane_stream` to create a controller or observer.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        controller: bool,
    ) -> None:
        self._process = process
        self._controller = controller
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = process.stderr
        self._write_lock = asyncio.Lock()
        self._release_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._stderr_tail = bytearray()
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._previous_seq: int | None = None
        self._received_closed = False
        self._closing = False
        self._closed = False
        self._released = False

    @property
    def pid(self) -> int:
        """Return the leader process ID."""
        return self._process.pid

    @property
    def closed(self) -> bool:
        """Return true after :meth:`close` finishes."""
        return self._closed

    async def _drain_stderr(self) -> None:
        while True:
            chunk = await self._stderr.read(64 * 1024)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > STDERR_TAIL_BYTES:
                del self._stderr_tail[:-STDERR_TAIL_BYTES]

    def _stderr_diagnostic(self) -> str:
        message = bytes(self._stderr_tail).decode("utf-8", errors="replace").strip()
        return f": {message}" if message else ""

    async def read_record(self) -> AnsiFrame | TerminalClosed:
        """Read and validate the next Herdr stdout record."""
        if self._closed or self._closing or self._received_closed:
            raise PaneStreamClosedError("terminal-session stream is closed")
        try:
            line = await self._stdout.readline()
        except ValueError as error:
            # StreamReader.readline() clears a line that exceeds its limit.
            raise _protocol_error("line exceeds the configured size limit") from error
        if not line:
            raise PaneStreamProtocolError(
                "terminal-session stream ended without terminal.closed"
                + self._stderr_diagnostic()
            )
        if len(line) > MAX_NDJSON_LINE_BYTES:
            raise _protocol_error("line exceeds the configured size limit")
        if not line.endswith(b"\n"):
            raise _protocol_error("record is not newline-delimited")
        record = _parse_record(line[:-1], self._previous_seq)
        if isinstance(record, AnsiFrame):
            self._previous_seq = record.seq
        else:
            self._received_closed = True
        return record

    async def __anext__(self) -> AnsiFrame | TerminalClosed:
        return await self.read_record()

    def __aiter__(self) -> AsyncIterator[AnsiFrame | TerminalClosed]:
        return self

    async def _write_command(
        self, command: dict[str, object], *, allow_closing: bool = False
    ) -> None:
        async with self._write_lock:
            if (
                self._closed
                or (self._closing and not allow_closing)
                or (self._released and command.get("type") != "terminal.release")
            ):
                raise PaneStreamClosedError("terminal-session stream is closed")
            try:
                encoded = json.dumps(command, separators=(",", ":")).encode("utf-8") + b"\n"
                self._stdin.write(encoded)
                await asyncio.wait_for(
                    self._stdin.drain(), timeout=COMMAND_DRAIN_TIMEOUT_SECONDS
                )
            except (BrokenPipeError, ConnectionError, OSError) as error:
                raise PaneStreamClosedError("terminal-session stream is closed") from error
            except asyncio.TimeoutError as error:
                raise PaneStreamClosedError("terminal-session command timed out") from error

    async def _release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            self._released = True
            try:
                await self._write_command({"type": "terminal.release"}, allow_closing=True)
            except PaneStreamClosedError:
                # Cleanup must continue if Herdr already closed its input pipe.
                pass

    @staticmethod
    def _process_group_exists(pid: int) -> bool:
        if os.name != "posix":
            return False
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            # A terminated child can briefly leave an unprobeable process
            # group on macOS. It cannot receive another signal from us.
            return False
        return True

    async def _wait_for_group_exit(self, timeout: float) -> bool:
        if os.name != "posix":
            try:
                await asyncio.wait_for(self._process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return False
            return True
        deadline = asyncio.get_running_loop().time() + timeout
        while self._process_group_exists(self._process.pid):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    async def _stop_process_group(self) -> None:
        if self._process.stdin is not None:
            self._stdin.close()
            try:
                await self._stdin.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

        # Give Herdr a short chance to act on terminal.release before signals
        # stop its group. This remains bounded when the child ignores input.
        if self._controller and self._process.returncode is None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=RELEASE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass

        if os.name == "posix":
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            exited = await self._wait_for_group_exit(TERMINATE_GRACE_SECONDS)
            if not exited:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await self._wait_for_group_exit(KILL_GRACE_SECONDS)
        elif self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                self._process.kill()

        try:
            await asyncio.wait_for(self._process.wait(), timeout=KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logger.error("could not reap terminal-session process %s", self._process.pid)

    async def close(self) -> None:
        """Release control, stop the child group, and reap its leader."""
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            if self._controller:
                await self._release()
            await self._stop_process_group()
            try:
                await asyncio.wait_for(self._stderr_task, timeout=KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._closed = True
            self._closing = False


class PaneController(PaneStream):
    """A terminal-session stream that can send terminal input and resize commands."""

    async def send_input(self, data: bytes | bytearray | memoryview) -> None:
        """Send terminal input as base64 bytes."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("terminal input must be bytes")
        await self._write_command(
            {
                "type": "terminal.input",
                "bytes": base64.b64encode(bytes(data)).decode("ascii"),
            }
        )

    async def resize(self, cols: int, rows: int) -> None:
        """Set the terminal dimensions."""
        cols = _terminal_dimension(cols, "cols")
        rows = _terminal_dimension(rows, "rows")
        await self._write_command(
            {"type": "terminal.resize", "cols": cols, "rows": rows}
        )

    async def scroll(
        self,
        direction: str,
        lines: int,
        *,
        source: str = "wheel",
        column: int | None = None,
        row: int | None = None,
        modifiers: int = 0,
    ) -> None:
        """Scroll the attached terminal through Herdr's public control command."""
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        if not _is_integer(lines) or lines <= 0:
            raise ValueError("lines must be a positive integer")
        if source not in {"wheel", "page_key"}:
            raise ValueError("source must be wheel or page_key")
        if column is not None and (not _is_integer(column) or not 0 <= column <= 65535):
            raise ValueError("column must be an integer from 0 to 65535")
        if row is not None and (not _is_integer(row) or not 0 <= row <= 65535):
            raise ValueError("row must be an integer from 0 to 65535")
        if not _is_integer(modifiers) or not 0 <= modifiers <= 255:
            raise ValueError("modifiers must be an integer from 0 to 255")
        command: dict[str, object] = {
            "type": "terminal.scroll",
            "direction": direction,
            "lines": lines,
            "source": source,
            "modifiers": modifiers,
        }
        if column is not None:
            command["column"] = column
        if row is not None:
            command["row"] = row
        await self._write_command(command)

    async def release(self) -> None:
        """Release this controller's terminal-session lease."""
        await self._release()


class PaneObserver(PaneStream):
    """A read-only terminal-session stream."""

    async def send_input(self, data: bytes | bytearray | memoryview) -> None:
        """Reject terminal input for an observer."""
        raise PermissionError("terminal observers cannot send input")

    async def resize(self, cols: int, rows: int) -> None:
        """Reject resize commands for an observer."""
        raise PermissionError("terminal observers cannot resize")

    async def scroll(self, direction: str, lines: int, **_options: object) -> None:
        """Reject scroll commands for an observer."""
        raise PermissionError("terminal observers cannot scroll")


async def open_pane_stream(
    pane_id: str,
    *,
    cols: int,
    rows: int,
    executable: str,
    environment: Mapping[str, str],
    control: bool = False,
) -> PaneController | PaneObserver:
    """Start a public Herdr terminal-session controller or observer.

    The supplied environment replaces the parent environment for the child.
    """
    if not isinstance(pane_id, str) or not pane_id:
        raise ValueError("pane_id must be a non-empty string")
    cols = _terminal_dimension(cols, "cols")
    rows = _terminal_dimension(rows, "rows")
    if not isinstance(executable, str) or not executable:
        raise ValueError("executable must be a non-empty string")
    child_environment = dict(environment)
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in child_environment.items()
    ):
        raise TypeError("environment keys and values must be strings")
    mode = "control" if control else "observe"
    process = await asyncio.create_subprocess_exec(
        executable,
        "terminal",
        "session",
        mode,
        pane_id,
        "--cols",
        str(cols),
        "--rows",
        str(rows),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_environment,
        limit=MAX_NDJSON_LINE_BYTES,
        start_new_session=os.name == "posix",
    )
    if control:
        return PaneController(process, controller=True)
    return PaneObserver(process, controller=False)
