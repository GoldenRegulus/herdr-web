import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, call, patch

from herdr_web.app import (
    Backend,
    MAX_PANE_TEXT_PASTE_BYTES,
    PANE_FRAME_HEADER,
    PANE_FRAME_MAGIC,
    encode_pane_mouse_sequence,
    run_herdr_socket_api,
    run_panes_websocket,
    validate_pane_paste_text,
)


class FakeWebSocket:
    def __init__(
        self,
        *,
        acknowledge: bool = True,
        after_frame: list[dict[str, object]] | None = None,
    ) -> None:
        self.acknowledge = acknowledge
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
