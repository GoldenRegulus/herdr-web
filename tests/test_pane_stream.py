import asyncio
import base64
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from herdr_web.pane_stream import (
    AnsiFrame,
    PaneController,
    PaneObserver,
    PaneStreamProtocolError,
    TerminalClosed,
    open_pane_stream,
)


class PaneStreamTests(unittest.IsolatedAsyncioTestCase):
    def make_executable(self, body: str) -> Path:
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(directory.cleanup)
        executable = Path(directory.name) / "fake-herdr"
        executable.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        executable.chmod(0o700)
        return executable

    async def wait_for_file(self, path: Path) -> None:
        for _ in range(100):
            if path.exists():
                return
            await asyncio.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    async def test_selects_command_environment_and_new_session(self) -> None:
        executable = self.make_executable(
            """import json
import os
import sys

Path = __import__('pathlib').Path
Path(os.environ['RESULT_PATH']).write_text(json.dumps({
    'argv': sys.argv[1:],
    'marker': os.environ.get('MARKER'),
    'pid': os.getpid(),
    'pgrp': os.getpgrp(),
    'sid': os.getsid(0),
}))
print(json.dumps({'type': 'terminal.closed', 'reason': 'done'}), flush=True)
"""
        )
        result = executable.parent / "result.json"
        stream = await open_pane_stream(
            "pane-7",
            cols=80,
            rows=24,
            executable=str(executable),
            environment={"RESULT_PATH": str(result), "MARKER": "selected"},
            control=True,
        )
        self.addAsyncCleanup(stream.close)

        self.assertIsInstance(stream, PaneController)
        self.assertEqual(await stream.read_record(), TerminalClosed("done"))
        await self.wait_for_file(result)
        launched = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(
            launched["argv"],
            ["terminal", "session", "control", "pane-7", "--cols", "80", "--rows", "24"],
        )
        self.assertEqual(launched["marker"], "selected")
        if os.name == "posix":
            self.assertEqual(launched["pid"], launched["pgrp"])
            self.assertEqual(launched["pid"], launched["sid"])

        observer_result = executable.parent / "observer-result.json"
        observer = await open_pane_stream(
            "pane-8",
            cols=100,
            rows=30,
            executable=str(executable),
            environment={"RESULT_PATH": str(observer_result), "MARKER": "observe"},
        )
        self.addAsyncCleanup(observer.close)
        self.assertIsInstance(observer, PaneObserver)
        self.assertEqual(await observer.read_record(), TerminalClosed("done"))
        await self.wait_for_file(observer_result)
        observed = json.loads(observer_result.read_text(encoding="utf-8"))
        self.assertEqual(
            observed["argv"],
            ["terminal", "session", "observe", "pane-8", "--cols", "100", "--rows", "30"],
        )
        self.assertEqual(observed["marker"], "observe")

    async def test_parses_ansi_frame_then_closed(self) -> None:
        executable = self.make_executable(
            """import base64
import json

print(json.dumps({
    'type': 'terminal.frame', 'seq': 4, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'\\x1b[31mred').decode('ascii'),
}), flush=True)
print(json.dumps({'type': 'terminal.closed', 'reason': 'pane child exited'}), flush=True)
"""
        )
        stream = await open_pane_stream(
            "pane-1",
            cols=80,
            rows=24,
            executable=str(executable),
            environment={},
        )
        self.addAsyncCleanup(stream.close)

        frame = await stream.read_record()
        closed = await stream.read_record()

        self.assertIsInstance(frame, AnsiFrame)
        self.assertEqual(frame.seq, 4)
        self.assertEqual((frame.width, frame.height, frame.full), (80, 24, True))
        self.assertEqual(frame.bytes, b"\x1b[31mred")
        self.assertEqual(closed, TerminalClosed("pane child exited"))

    async def test_controller_serializes_input_resize_scroll_and_release(self) -> None:
        executable = self.make_executable(
            """import json
import os
from pathlib import Path
import sys

records = []
for line in sys.stdin:
    records.append(json.loads(line))
    if records[-1]['type'] == 'terminal.release':
        Path(os.environ['RESULT_PATH']).write_text(json.dumps(records))
        break
"""
        )
        result = executable.parent / "commands.json"
        stream = await open_pane_stream(
            "pane-2",
            cols=80,
            rows=24,
            executable=str(executable),
            environment={"RESULT_PATH": str(result)},
            control=True,
        )
        self.addAsyncCleanup(stream.close)

        await stream.send_input(b"a\x00b")
        await stream.resize(120, 40)
        await stream.scroll("up", 3)
        await asyncio.sleep(0.05)
        await stream.release()
        await stream.close()
        await self.wait_for_file(result)

        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")),
            [
                {
                    "type": "terminal.input",
                    "bytes": base64.b64encode(b"a\x00b").decode("ascii"),
                },
                {"type": "terminal.resize", "cols": 120, "rows": 40},
                {
                    "type": "terminal.scroll",
                    "direction": "up",
                    "lines": 3,
                    "source": "wheel",
                    "modifiers": 0,
                },
                {"type": "terminal.release"},
            ],
        )

    async def test_observer_rejects_input_and_resize(self) -> None:
        executable = self.make_executable(
            """import sys
for line in sys.stdin:
    pass
"""
        )
        stream = await open_pane_stream(
            "pane-3",
            cols=80,
            rows=24,
            executable=str(executable),
            environment={},
        )
        self.addAsyncCleanup(stream.close)

        self.assertIsInstance(stream, PaneObserver)
        with self.assertRaises(PermissionError):
            await stream.send_input(b"x")
        with self.assertRaises(PermissionError):
            await stream.resize(100, 30)
        with self.assertRaises(PermissionError):
            await stream.scroll("up", 3)

    async def test_rejects_malformed_and_oversize_frames(self) -> None:
        malformed = self.make_executable(
            """import json
print(json.dumps({
    'type': 'terminal.frame', 'seq': 0, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'text', 'bytes': 'eA=='
}), flush=True)
"""
        )
        stream = await open_pane_stream(
            "pane-4",
            cols=80,
            rows=24,
            executable=str(malformed),
            environment={},
        )
        self.addAsyncCleanup(stream.close)
        with self.assertRaises(PaneStreamProtocolError):
            await stream.read_record()

        oversized = self.make_executable(
            """import base64
import json
print(json.dumps({
    'type': 'terminal.frame', 'seq': 0, 'width': 80, 'height': 24,
    'full': False, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'12345').decode('ascii')
}), flush=True)
"""
        )
        with patch("herdr_web.pane_stream.MAX_FRAME_BYTES", 4):
            stream = await open_pane_stream(
                "pane-5",
                cols=80,
                rows=24,
                executable=str(oversized),
                environment={},
            )
            self.addAsyncCleanup(stream.close)
            with self.assertRaises(PaneStreamProtocolError):
                await stream.read_record()

    async def test_close_reaps_the_process_group(self) -> None:
        executable = self.make_executable(
            """import os
from pathlib import Path
import time

child = os.fork()
if child == 0:
    while True:
        time.sleep(1)
Path(os.environ['CHILD_PID_PATH']).write_text(str(child))
while True:
    time.sleep(1)
"""
        )
        child_path = executable.parent / "child.pid"
        stream = await open_pane_stream(
            "pane-6",
            cols=80,
            rows=24,
            executable=str(executable),
            environment={"CHILD_PID_PATH": str(child_path)},
            control=True,
        )
        await self.wait_for_file(child_path)
        child_pid = int(child_path.read_text(encoding="utf-8"))
        leader_pid = stream.pid

        await stream.close()
        await stream.close()

        if os.name == "posix":
            with self.assertRaises(ProcessLookupError):
                os.killpg(leader_pid, 0)
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("child process was not reaped")


if __name__ == "__main__":
    unittest.main()
