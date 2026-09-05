import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import zlib
from unittest.mock import AsyncMock, call, patch

from herdr_web.app import (
    AdaptivePaneFramePacer,
    Backend,
    MAX_PANE_TEXT_PASTE_BYTES,
    PANE_DEFLATE_COOPERATIVE_BYTES,
    PANE_FRAME_FLAG_DEFLATE,
    PANE_FRAME_FLAG_FULL,
    PANE_FRAME_HEADER,
    PANE_FRAME_MAGIC,
    PANE_MAX_SEND_FRAMES_PER_SECOND,
    PANE_MIN_SEND_FRAMES_PER_SECOND,
    PaneFrameScheduler,
    PaneOutputFrame,
    encode_pane_mouse_sequence,
    encode_pane_websocket_frame,
    encode_pane_websocket_frame_async,
    run_herdr_socket_api,
    run_panes_websocket,
    validate_pane_paste_text,
)
from herdr_web.pane_stream import AnsiFrame


class FakeWebSocket:
    def __init__(
        self,
        *,
        acknowledge: bool = True,
        acknowledge_delay: float = 0,
        after_frame: list[dict[str, object]] | None = None,
    ) -> None:
        self.acknowledge = acknowledge
        self.acknowledge_delay = acknowledge_delay
        self.after_frame = after_frame
        self.sent_json: list[dict[str, object]] = []
        self.sent_bytes: list[bytes] = []
        self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.closed: tuple[int, str] | None = None

    async def send_json(self, message: dict[str, object]) -> None:
        self.sent_json.append(message)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)
        magic, stream_id, seq, _full, _width, _height = PANE_FRAME_HEADER.unpack(
            data[: PANE_FRAME_HEADER.size]
        )
        assert magic == PANE_FRAME_MAGIC
        if not self.acknowledge:
            return
        if self.acknowledge_delay:
            await asyncio.sleep(self.acknowledge_delay)
        await self.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "pane-output-ack",
                        "stream_id": stream_id,
                        "seq": str(seq),
                    }
                ),
            }
        )
        follow_up = self.after_frame
        if follow_up is None:
            follow_up = [
                {"type": "websocket.receive", "bytes": b"browser input"},
                {"type": "websocket.disconnect", "code": 1000},
            ]
        for message in follow_up:
            await self.incoming.put(message)

    async def receive(self) -> dict[str, object]:
        return await self.incoming.get()

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


class PaneWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def test_negotiated_pane_frames_use_bounded_deflate(self) -> None:
        frame = AnsiFrame(
            seq=7,
            width=80,
            height=24,
            full=True,
            bytes=b"repeated terminal cells " * 500,
        )

        encoded = encode_pane_websocket_frame(3, frame, deflate=True)
        header = PANE_FRAME_HEADER.unpack(encoded[: PANE_FRAME_HEADER.size])

        self.assertEqual(header[:3], (PANE_FRAME_MAGIC, 3, 7))
        self.assertEqual(
            header[3], PANE_FRAME_FLAG_FULL | PANE_FRAME_FLAG_DEFLATE
        )
        self.assertEqual(zlib.decompress(encoded[PANE_FRAME_HEADER.size :]), frame.bytes)

        small = AnsiFrame(8, 80, 24, True, b"small")
        raw = encode_pane_websocket_frame(3, small, deflate=True)
        raw_header = PANE_FRAME_HEADER.unpack(raw[: PANE_FRAME_HEADER.size])
        self.assertEqual(raw_header[3], PANE_FRAME_FLAG_FULL)
        self.assertEqual(raw[PANE_FRAME_HEADER.size :], b"small")

    async def test_large_pane_compression_yields_between_bounded_steps(self) -> None:
        frame = AnsiFrame(
            seq=9,
            width=80,
            height=24,
            full=True,
            bytes=b"compressible terminal frame " * PANE_DEFLATE_COOPERATIVE_BYTES,
        )

        cooperative_yield = AsyncMock()
        with patch("herdr_web.app.asyncio.sleep", cooperative_yield):
            encoded = await encode_pane_websocket_frame_async(
                4, frame, deflate=True
            )

        self.assertGreater(cooperative_yield.await_count, 1)
        header = PANE_FRAME_HEADER.unpack(encoded[: PANE_FRAME_HEADER.size])
        self.assertEqual(
            header[3], PANE_FRAME_FLAG_FULL | PANE_FRAME_FLAG_DEFLATE
        )

    def test_frame_scheduler_prioritizes_input_without_starving_background(self) -> None:
        scheduler = PaneFrameScheduler([1, 2])

        def item(stream_id: int, seq: int) -> PaneOutputFrame:
            return PaneOutputFrame(
                stream_id,
                AnsiFrame(seq, 80, 24, seq == 1, b"frame"),
                asyncio.Event(),
            )

        scheduler.publish(item(1, 1))
        scheduler.publish(item(2, 1))
        scheduler.prioritize(2)
        self.assertEqual(scheduler.take_next().stream_id, 2)

        for seq in range(2, 5):
            scheduler.publish(item(2, seq))
            self.assertEqual(scheduler.take_next().stream_id, 2)
        scheduler.publish(item(2, 5))
        self.assertEqual(scheduler.take_next().stream_id, 1)
        self.assertEqual(scheduler.take_next().stream_id, 2)

    def test_frame_pacer_adapts_per_client_acknowledgement_time(self) -> None:
        fast = AdaptivePaneFramePacer()
        fast.note_sent(10)
        fast.note_acknowledged(0.001)
        self.assertAlmostEqual(
            fast.target_frames_per_second, PANE_MAX_SEND_FRAMES_PER_SECOND
        )

        constrained = AdaptivePaneFramePacer()
        constrained.note_sent(10)
        constrained.note_acknowledged(0.2)
        self.assertAlmostEqual(constrained.target_frames_per_second, 4)

        slower_than_budget = AdaptivePaneFramePacer()
        slower_than_budget.note_sent(10)
        slower_than_budget.note_acknowledged(2)
        self.assertAlmostEqual(
            slower_than_budget.target_frames_per_second,
            PANE_MIN_SEND_FRAMES_PER_SECOND,
        )

    async def test_interactive_input_can_expedite_the_frame_pacer(self) -> None:
        pacer = AdaptivePaneFramePacer()
        pacer.note_sent(time.monotonic())
        pacer.note_acknowledged(0.2)
        waiter = asyncio.create_task(pacer.wait())
        await asyncio.sleep(0.01)
        self.assertFalse(waiter.done())

        pacer.expedite()
        await asyncio.wait_for(waiter, timeout=0.1)

        in_flight = AdaptivePaneFramePacer()
        in_flight.note_sent(time.monotonic())
        in_flight.expedite()
        in_flight.note_acknowledged(0.2)
        await asyncio.wait_for(in_flight.wait(), timeout=0.01)

    async def wait_for_frames(self, websocket: FakeWebSocket, count: int) -> None:
        for _ in range(200):
            if len(websocket.sent_bytes) >= count:
                return
            await asyncio.sleep(0.005)
        self.fail(f"timed out waiting for {count} pane frames")

    async def acknowledge_sequence(
        self, websocket: FakeWebSocket, stream_id: int, seq: int
    ) -> None:
        await websocket.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "pane-output-ack",
                        "stream_id": stream_id,
                        "seq": str(seq),
                    }
                ),
            }
        )

    async def acknowledge_frame(self, websocket: FakeWebSocket, data: bytes) -> None:
        _, stream_id, seq, _, _, _ = PANE_FRAME_HEADER.unpack(
            data[: PANE_FRAME_HEADER.size]
        )
        await self.acknowledge_sequence(websocket, stream_id, seq)

    async def test_multiplexes_frame_ack_and_input(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            commands_path = directory / "commands.json"
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import os
from pathlib import Path
import sys

print(json.dumps({{
    'type': 'terminal.frame', 'seq': 9, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'frame bytes').decode('ascii'),
}}), flush=True)
records = []
for line in sys.stdin:
    records.append(json.loads(line))
    Path(os.environ['COMMANDS_PATH']).write_text(json.dumps(records))
    if records[-1]['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            websocket = FakeWebSocket()
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "compression": "deflate",
                "panes": [
                    {"stream_id": 3, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            environment = {
                "HERDR_BINARY": str(executable),
                "COMMANDS_PATH": str(commands_path),
            }
            with (
                patch.dict(os.environ, environment),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
            ):
                await asyncio.wait_for(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    ),
                    timeout=5,
                )

            self.assertEqual(websocket.sent_json[0]["type"], "panes-attached")
            self.assertEqual(websocket.sent_json[0]["compression"], "deflate")
            self.assertEqual(len(websocket.sent_bytes), 1)
            header = PANE_FRAME_HEADER.unpack(
                websocket.sent_bytes[0][: PANE_FRAME_HEADER.size]
            )
            self.assertEqual(header, (PANE_FRAME_MAGIC, 3, 9, 1, 80, 24))
            self.assertEqual(
                websocket.sent_bytes[0][PANE_FRAME_HEADER.size :], b"frame bytes"
            )
            for _ in range(100):
                if commands_path.exists():
                    records = json.loads(commands_path.read_text(encoding="utf-8"))
                    if any(record["type"] == "terminal.release" for record in records):
                        break
                await asyncio.sleep(0.01)
            else:
                self.fail("pane controller did not release")
            input_record = next(
                record for record in records if record["type"] == "terminal.input"
            )
            self.assertEqual(
                base64.b64decode(input_record["bytes"], validate=True), b"browser input"
            )

    async def test_only_one_pane_frame_is_unacknowledged_across_streams(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import sys

pane = sys.argv[4]
print(json.dumps({{
    'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(pane.encode()).decode('ascii'),
}}), flush=True)
for line in sys.stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [
                    {"pane_id": "p1", "tab_id": "t1"},
                    {"pane_id": "p2", "tab_id": "t1"},
                ],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24},
                    {"stream_id": 2, "pane_id": "p2", "cols": 80, "rows": 24},
                ],
            }
            websocket = FakeWebSocket(acknowledge=False)
            with (
                patch.dict(os.environ, {"HERDR_BINARY": str(executable)}),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 1)
                await asyncio.sleep(0.05)
                self.assertEqual(len(websocket.sent_bytes), 1)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[0])
                await self.wait_for_frames(websocket, 2)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[1])
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=5)

            stream_ids = {
                PANE_FRAME_HEADER.unpack(data[: PANE_FRAME_HEADER.size])[1]
                for data in websocket.sent_bytes
            }
            self.assertEqual(stream_ids, {1, 2})

    async def test_error_reporting_does_not_block_parser_ack_input(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import sys

print(json.dumps({{
    'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(sys.argv[4].encode()).decode('ascii'),
}}), flush=True)
for line in sys.stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [
                    {"pane_id": "p1", "tab_id": "t1"},
                    {"pane_id": "p2", "tab_id": "t1"},
                ],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24},
                    {"stream_id": 2, "pane_id": "p2", "cols": 80, "rows": 24},
                ],
            }
            websocket = FakeWebSocket(acknowledge=False)
            with (
                patch.dict(os.environ, {"HERDR_BINARY": str(executable)}),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 1)
                await asyncio.sleep(0.05)
                first_header = PANE_FRAME_HEADER.unpack(
                    websocket.sent_bytes[0][: PANE_FRAME_HEADER.size]
                )
                await websocket.incoming.put(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "pane-resize",
                                "stream_id": first_header[1],
                                "cols": 0,
                                "rows": 24,
                            }
                        ),
                    }
                )
                await self.acknowledge_frame(websocket, websocket.sent_bytes[0])
                await self.wait_for_frames(websocket, 2)
                for _ in range(200):
                    if any(
                        message.get("type") == "error"
                        for message in websocket.sent_json
                    ):
                        break
                    await asyncio.sleep(0.005)
                else:
                    self.fail("timed out waiting for the queued pane error")
                await self.acknowledge_frame(websocket, websocket.sent_bytes[1])
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=5)

    async def test_slow_command_does_not_block_parser_ack_intake(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import time


def frame(seq):
    print(json.dumps({{
        'type': 'terminal.frame', 'seq': seq, 'width': 80, 'height': 24,
        'full': seq == 1, 'encoding': 'ansi',
        'bytes': base64.b64encode(f'frame-{{seq}}'.encode()).decode('ascii'),
    }}), flush=True)


frame(1)
time.sleep(0.08)
frame(2)
time.sleep(0.12)
frame(3)
for line in __import__('sys').stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            websocket = FakeWebSocket(acknowledge=False)
            api_started = asyncio.Event()
            release_api = asyncio.Event()

            async def slow_api(*_args, **_kwargs):
                api_started.set()
                await release_api.wait()
                return {"id": "test", "result": {"type": "ok"}}

            with (
                patch.dict(os.environ, {"HERDR_BINARY": str(executable)}),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.run_herdr_socket_api", side_effect=slow_api),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 1)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[0])
                await self.wait_for_frames(websocket, 2)
                await websocket.incoming.put(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "pane-paste",
                                "stream_id": 1,
                                "text": "slow paste",
                            }
                        ),
                    }
                )
                await asyncio.wait_for(api_started.wait(), timeout=1)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[1])
                await self.wait_for_frames(websocket, 3)
                self.assertFalse(release_api.is_set())
                await self.acknowledge_frame(websocket, websocket.sent_bytes[2])
                release_api.set()
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=5)

    async def test_command_queue_overflow_closes_without_blocking_ack_intake(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json

print(json.dumps({{
    'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'frame').decode('ascii'),
}}), flush=True)
for line in __import__('sys').stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            websocket = FakeWebSocket(acknowledge=False)
            api_started = asyncio.Event()

            async def slow_api(*_args, **_kwargs):
                api_started.set()
                await asyncio.Event().wait()

            with (
                patch.dict(os.environ, {"HERDR_BINARY": str(executable)}),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.run_herdr_socket_api", side_effect=slow_api),
                patch("herdr_web.app.PANE_COMMAND_QUEUE_SIZE", 1),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 1)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[0])
                await websocket.incoming.put(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "pane-paste",
                                "stream_id": 1,
                                "text": "blocked",
                            }
                        ),
                    }
                )
                await asyncio.wait_for(api_started.wait(), timeout=1)
                queued = {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {"type": "pane-active", "stream_id": 1}
                    ),
                }
                await websocket.incoming.put(queued)
                await websocket.incoming.put(queued)
                await asyncio.wait_for(task, timeout=5)

            self.assertEqual(websocket.closed, (1011, "pane command queue is full"))

    async def test_ack_for_a_queued_frame_does_not_release_the_sender(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import sys
import time

pane = sys.argv[4]
for seq in (1, 2):
    print(json.dumps({{
        'type': 'terminal.frame', 'seq': seq, 'width': 80, 'height': 24,
        'full': seq == 1, 'encoding': 'ansi',
        'bytes': base64.b64encode(f'{{pane}}-{{seq}}'.encode()).decode('ascii'),
    }}), flush=True)
    time.sleep(0.15)
for line in sys.stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [
                    {"pane_id": "p1", "tab_id": "t1"},
                    {"pane_id": "p2", "tab_id": "t1"},
                ],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24},
                    {"stream_id": 2, "pane_id": "p2", "cols": 80, "rows": 24},
                ],
            }
            websocket = FakeWebSocket(acknowledge=False)
            with (
                patch.dict(os.environ, {"HERDR_BINARY": str(executable)}),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 1)
                first_header = PANE_FRAME_HEADER.unpack(
                    websocket.sent_bytes[0][: PANE_FRAME_HEADER.size]
                )
                queued_stream_id = 2 if first_header[1] == 1 else 1
                await asyncio.sleep(0.02)
                await self.acknowledge_sequence(websocket, queued_stream_id, 1)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[0])
                await self.wait_for_frames(websocket, 2)
                second_header = PANE_FRAME_HEADER.unpack(
                    websocket.sent_bytes[1][: PANE_FRAME_HEADER.size]
                )
                self.assertEqual((second_header[1], second_header[2]), (queued_stream_id, 1))
                await asyncio.sleep(0.05)
                self.assertEqual(len(websocket.sent_bytes), 2)

                await self.acknowledge_frame(websocket, websocket.sent_bytes[1])
                await self.wait_for_frames(websocket, 3)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[2])
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=5)

    async def test_slow_frame_fast_forwards_to_a_new_full_frame(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            commands_path = directory / "commands.json"
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import os
from pathlib import Path
import sys


def frame(seq, full, data, width=80):
    print(json.dumps({{
        'type': 'terminal.frame', 'seq': seq, 'width': width, 'height': 24,
        'full': full, 'encoding': 'ansi',
        'bytes': base64.b64encode(data).decode('ascii'),
    }}), flush=True)


frame(1, True, b'initial')
frame(2, False, b'stale incremental frame')
records = []
for line in sys.stdin:
    record = json.loads(line)
    records.append(record)
    Path(os.environ['COMMANDS_PATH']).write_text(json.dumps(records))
    if record['type'] == 'terminal.resize':
        frame(3, True, b'wrong-size full frame', width=79)
        frame(4, True, b'fresh full frame')
    if record['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            websocket = FakeWebSocket(
                acknowledge_delay=0.03,
                after_frame=[],
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "HERDR_BINARY": str(executable),
                        "COMMANDS_PATH": str(commands_path),
                    },
                ),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.PANE_RESYNC_TRIGGER_SECONDS", 0.01),
                patch("herdr_web.app.PANE_FULL_RESYNC_TIMEOUT_SECONDS", 1),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 2)
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=5)

            frames = [
                (
                    PANE_FRAME_HEADER.unpack(data[: PANE_FRAME_HEADER.size]),
                    data[PANE_FRAME_HEADER.size :],
                )
                for data in websocket.sent_bytes
            ]
            self.assertEqual([header[2] for header, _ in frames], [1, 4])
            self.assertEqual([header[3] for header, _ in frames], [1, 1])
            self.assertEqual(
                [payload for _, payload in frames],
                [b"initial", b"fresh full frame"],
            )
            records = json.loads(commands_path.read_text(encoding="utf-8"))
            resize = next(
                record for record in records if record["type"] == "terminal.resize"
            )
            self.assertEqual((resize["cols"], resize["rows"]), (80, 24))

    async def test_slow_observer_reopens_only_as_an_observer(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            launches_path = directory / "launches.json"
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import os
from pathlib import Path
import sys

path = Path(os.environ['LAUNCHES_PATH'])
try:
    launches = json.loads(path.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    launches = []
mode = sys.argv[3]
launches.append({{'mode': mode, 'arguments': sys.argv[1:]}})
path.write_text(json.dumps(launches))
if mode == 'control':
    print(json.dumps({{
        'type': 'terminal.closed',
        'reason': 'already has an attached client',
    }}), flush=True)
else:
    count = sum(record['mode'] == 'observe' for record in launches)
    print(json.dumps({{
        'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
        'full': True, 'encoding': 'ansi',
        'bytes': base64.b64encode(f'observe-{{count}}'.encode()).decode('ascii'),
    }}), flush=True)
for line in sys.stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            websocket = FakeWebSocket(
                acknowledge_delay=0.03,
                after_frame=[],
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "HERDR_BINARY": str(executable),
                        "LAUNCHES_PATH": str(launches_path),
                    },
                ),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.PANE_RESYNC_TRIGGER_SECONDS", 0.01),
                patch("herdr_web.app.PANE_FULL_RESYNC_TIMEOUT_SECONDS", 1),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 2)
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=5)

            payloads = [
                data[PANE_FRAME_HEADER.size :] for data in websocket.sent_bytes
            ]
            self.assertEqual(payloads, [b"observe-1", b"observe-2"])
            launches = json.loads(launches_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [record["mode"] for record in launches],
                ["control", "observe", "observe"],
            )
            self.assertFalse(
                any(
                    "--takeover" in record["arguments"]
                    for record in launches
                )
            )
            self.assertTrue(
                any(
                    message.get("type") == "pane-mode"
                    and message.get("mode") == "observe"
                    for message in websocket.sent_json
                )
            )

    async def test_buffered_observer_frames_reopen_at_current_full_state(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            launches_path = directory / "launches.json"
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import os
from pathlib import Path
import sys

path = Path(os.environ['LAUNCHES_PATH'])
try:
    launches = json.loads(path.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    launches = []
mode = sys.argv[3]
launches.append(mode)
path.write_text(json.dumps(launches))
if mode == 'control':
    print(json.dumps({{
        'type': 'terminal.closed',
        'reason': 'already has an attached client',
    }}), flush=True)
else:
    count = launches.count('observe')
    records = [(1, True, f'observe-{{count}}-current')]
    if count == 1:
        records.extend((seq, False, f'stale-{{seq}}') for seq in range(2, 9))
    for seq, full, text in records:
        print(json.dumps({{
            'type': 'terminal.frame', 'seq': seq, 'width': 80, 'height': 24,
            'full': full, 'encoding': 'ansi',
            'bytes': base64.b64encode(text.encode()).decode('ascii'),
        }}), flush=True)
for line in sys.stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            websocket = FakeWebSocket(
                acknowledge_delay=0.04,
                after_frame=[],
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "HERDR_BINARY": str(executable),
                        "LAUNCHES_PATH": str(launches_path),
                    },
                ),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.PANE_RESYNC_TRIGGER_SECONDS", 0.05),
                patch("herdr_web.app.PANE_BUFFERED_FRAME_SECONDS", 0.005),
                patch("herdr_web.app.PANE_FULL_RESYNC_TIMEOUT_SECONDS", 1),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                for _ in range(300):
                    payloads = [
                        data[PANE_FRAME_HEADER.size :].decode()
                        for data in websocket.sent_bytes
                    ]
                    if payloads and payloads[-1] == "observe-2-current":
                        break
                    await asyncio.sleep(0.005)
                else:
                    self.fail("observer did not replace buffered history")
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=5)

            payloads = [
                data[PANE_FRAME_HEADER.size :].decode()
                for data in websocket.sent_bytes
            ]
            self.assertEqual(payloads[0], "observe-1-current")
            self.assertEqual(payloads[-1], "observe-2-current")
            self.assertLess(len(payloads), 8)
            launches = json.loads(launches_path.read_text(encoding="utf-8"))
            self.assertEqual(launches, ["control", "observe", "observe"])

    def test_validates_bounded_pane_paste_text(self) -> None:
        self.assertEqual(validate_pane_paste_text("first\nsecond"), "first\nsecond")
        self.assertEqual(
            validate_pane_paste_text("x" * MAX_PANE_TEXT_PASTE_BYTES),
            "x" * MAX_PANE_TEXT_PASTE_BYTES,
        )
        with self.assertRaisesRegex(ValueError, "invalid"):
            validate_pane_paste_text("")
        with self.assertRaisesRegex(ValueError, "too large"):
            validate_pane_paste_text("x" * (MAX_PANE_TEXT_PASTE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "Unicode"):
            validate_pane_paste_text("\ud800")

    def test_encodes_structured_mouse_operations_as_sgr(self) -> None:
        self.assertEqual(
            encode_pane_mouse_sequence(
                {
                    "button_code": 18,
                    "column": 7,
                    "row": 9,
                    "action": "click",
                }
            ),
            "\x1b[<18;7;9M\x1b[<18;7;9m",
        )
        with self.assertRaisesRegex(ValueError, "action"):
            encode_pane_mouse_sequence(
                {
                    "button_code": 2,
                    "column": 4,
                    "row": 3,
                    "action": "motion",
                }
            )
        with self.assertRaisesRegex(ValueError, "coordinates"):
            encode_pane_mouse_sequence(
                {"button_code": 2, "column": 0, "row": 3, "action": "click"}
            )

    async def test_direct_json_api_preserves_control_text(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            socket_path = directory / "herdr.sock"
            requests: list[dict[str, object]] = []

            async def handle(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                request = json.loads(await reader.readline())
                requests.append(request)
                writer.write(
                    json.dumps(
                        {"id": request["id"], "result": {"type": "ok"}}
                    ).encode("utf-8")
                    + b"\n"
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=str(socket_path))
            try:
                response = await run_herdr_socket_api(
                    Backend("backend", "test", directory / "herdr-client.sock"),
                    "pane.send_text",
                    {"pane_id": "w1:p1", "text": "\x1b[<2;7;9M\x1b[<2;7;9m"},
                )
            finally:
                server.close()
                await server.wait_closed()

            self.assertEqual(response["result"], {"type": "ok"})
            self.assertEqual(requests[0]["method"], "pane.send_text")
            self.assertEqual(
                requests[0]["params"],
                {"pane_id": "w1:p1", "text": "\x1b[<2;7;9M\x1b[<2;7;9m"},
            )

    async def test_mouse_text_and_image_use_pane_api_without_terminal_input(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            commands_path = directory / "commands.json"
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import os
from pathlib import Path

print(json.dumps({{
    'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'frame').decode('ascii'),
}}), flush=True)
records = []
for line in __import__('sys').stdin:
    records.append(json.loads(line))
    Path(os.environ['COMMANDS_PATH']).write_text(json.dumps(records))
    if records[-1]['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            mouse_message = {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "pane-mouse",
                        "stream_id": 3,
                        "button_code": 2,
                        "column": 7,
                        "row": 9,
                        "action": "click",
                    }
                ),
            }
            paste_message = {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "pane-paste",
                        "stream_id": 3,
                        "text": "first\nsecond",
                    }
                ),
            }
            image_data = b"synthetic png bytes"
            image_header = {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "clipboard-image",
                        "stream_id": 3,
                        "extension": "png",
                        "size": len(image_data),
                    }
                ),
            }
            websocket = FakeWebSocket(
                after_frame=[
                    mouse_message,
                    paste_message,
                    image_header,
                    {"type": "websocket.receive", "bytes": image_data},
                    {"type": "websocket.disconnect", "code": 1000},
                ]
            )
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 3, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            backend = Backend("backend", "test", directory / "herdr-client.sock")
            observed_image: dict[str, object] = {}

            async def send_api(
                _backend: Backend, method: str, params: dict[str, object]
            ) -> dict[str, object]:
                text = params.get("text")
                if method == "pane.send_input" and text != "first\nsecond":
                    path = Path(str(text))
                    observed_image.update(
                        path=path,
                        existed=path.is_file(),
                        data=path.read_bytes(),
                    )
                return {"id": "test", "result": {"type": "ok"}}

            api_sender = AsyncMock(side_effect=send_api)
            with (
                patch.dict(
                    os.environ,
                    {"HERDR_BINARY": str(executable), "COMMANDS_PATH": str(commands_path)},
                ),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.run_herdr_socket_api", api_sender),
                patch("herdr_web.app.STAGED_IMAGE_DIRECTORY", directory / "images"),
            ):
                await asyncio.wait_for(
                    run_panes_websocket(websocket, backend, initial), timeout=5
                )

            self.assertEqual(api_sender.await_count, 3)
            api_sender.assert_has_awaits(
                [
                    call(
                        backend,
                        "pane.send_text",
                        {"pane_id": "p1", "text": "\x1b[<2;7;9M\x1b[<2;7;9m"},
                    ),
                    call(
                        backend,
                        "pane.send_input",
                        {"pane_id": "p1", "text": "first\nsecond"},
                    ),
                ]
            )
            image_call = api_sender.await_args_list[2]
            self.assertEqual(image_call.args[:2], (backend, "pane.send_input"))
            self.assertEqual(image_call.args[2]["pane_id"], "p1")
            self.assertEqual(observed_image["existed"], True)
            self.assertEqual(observed_image["data"], image_data)
            self.assertFalse(Path(observed_image["path"]).exists())
            for _ in range(100):
                if commands_path.exists():
                    records = json.loads(commands_path.read_text(encoding="utf-8"))
                    if any(record["type"] == "terminal.release" for record in records):
                        break
                await asyncio.sleep(0.01)
            else:
                self.fail("pane controller did not release")
            self.assertFalse(
                any(record["type"] == "terminal.input" for record in records)
            )
            self.assertFalse(
                any(message.get("type") == "error" for message in websocket.sent_json)
            )

    async def test_disconnect_bounds_blocked_command_drain_and_releases_controller(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            commands_path = directory / "commands.json"
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import os
from pathlib import Path

print(json.dumps({{
    'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'frame').decode('ascii'),
}}), flush=True)
records = []
for line in __import__('sys').stdin:
    records.append(json.loads(line))
    Path(os.environ['COMMANDS_PATH']).write_text(json.dumps(records))
    if records[-1]['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            websocket = FakeWebSocket(acknowledge=False)
            api_started = asyncio.Event()
            api_cancelled = asyncio.Event()
            api_calls: list[str] = []

            async def blocked_api(
                _backend: Backend, _method: str, params: dict[str, object]
            ) -> dict[str, object]:
                text = str(params["text"])
                api_calls.append(text)
                if text == "blocked":
                    api_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        api_cancelled.set()
                return {"id": "test", "result": {"type": "ok"}}

            with (
                patch.dict(
                    os.environ,
                    {
                        "HERDR_BINARY": str(executable),
                        "COMMANDS_PATH": str(commands_path),
                    },
                ),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.run_herdr_socket_api", side_effect=blocked_api),
                patch("herdr_web.app.PANE_COMMAND_DRAIN_TIMEOUT_SECONDS", 0.02),
            ):
                task = asyncio.create_task(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    )
                )
                await self.wait_for_frames(websocket, 1)
                await self.acknowledge_frame(websocket, websocket.sent_bytes[0])
                for text in ("blocked", "must not run"):
                    await websocket.incoming.put(
                        {
                            "type": "websocket.receive",
                            "text": json.dumps(
                                {"type": "pane-paste", "stream_id": 1, "text": text}
                            ),
                        }
                    )
                await asyncio.wait_for(api_started.wait(), timeout=1)
                await asyncio.sleep(0.01)
                await websocket.incoming.put(
                    {"type": "websocket.disconnect", "code": 1000}
                )
                await asyncio.wait_for(task, timeout=1)

            self.assertTrue(api_cancelled.is_set())
            self.assertEqual(api_calls, ["blocked"])
            records = json.loads(commands_path.read_text(encoding="utf-8"))
            self.assertIn({"type": "terminal.release"}, records)

    async def test_blocked_pane_send_closes_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json

print(json.dumps({{
    'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'blocked send').decode('ascii'),
}}), flush=True)
for line in __import__('sys').stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }

            class BlockedWebSocket(FakeWebSocket):
                def __init__(self) -> None:
                    super().__init__(acknowledge=False)
                    self.close_cancelled = asyncio.Event()

                async def send_bytes(self, _data: bytes) -> None:
                    await asyncio.Event().wait()

                async def close(self, *, code: int, reason: str) -> None:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        self.closed = (code, reason)
                        self.close_cancelled.set()

            websocket = BlockedWebSocket()
            with (
                patch.dict(os.environ, {"HERDR_BINARY": str(executable)}),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.02),
            ):
                await asyncio.wait_for(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    ),
                    timeout=5,
                )

            self.assertEqual(
                websocket.closed,
                (1011, "pane WebSocket send timed out"),
            )
            self.assertTrue(websocket.close_cancelled.is_set())

    async def test_parser_ack_timeout_closes_the_websocket(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            directory = Path(directory_name)
            executable = directory / "fake-herdr"
            executable.write_text(
                f"""#!{sys.executable}
import base64
import json
import sys
print(json.dumps({{
    'type': 'terminal.frame', 'seq': 1, 'width': 80, 'height': 24,
    'full': True, 'encoding': 'ansi',
    'bytes': base64.b64encode(b'frame').decode('ascii'),
}}), flush=True)
for line in sys.stdin:
    if json.loads(line)['type'] == 'terminal.release':
        break
""",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            websocket = FakeWebSocket(acknowledge=False)
            snapshot = {
                "tabs": [{"tab_id": "t1"}],
                "panes": [{"pane_id": "p1", "tab_id": "t1"}],
            }
            initial = {
                "type": "panes.attach",
                "tab_id": "t1",
                "panes": [
                    {"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}
                ],
            }
            with (
                patch.dict(os.environ, {"HERDR_BINARY": str(executable)}),
                patch(
                    "herdr_web.app.navigation_snapshot",
                    AsyncMock(return_value=snapshot),
                ),
                patch("herdr_web.app.PANE_FRAME_ACK_TIMEOUT_SECONDS", 0.05),
            ):
                await asyncio.wait_for(
                    run_panes_websocket(
                        websocket,
                        Backend("backend", "test", directory / "herdr-client.sock"),
                        initial,
                    ),
                    timeout=3,
                )

            self.assertEqual(websocket.closed, (1011, "pane parser acknowledgement timed out"))


if __name__ == "__main__":
    unittest.main()
