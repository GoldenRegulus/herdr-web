import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from herdr_web.app import (
    Backend, MAX_CLIPBOARD_IMAGE_BYTES, MAX_PANE_TEXT_PASTE_BYTES,
    OUTPUT_ACK_WINDOW_BYTES, OUTPUT_WEBSOCKET_CHUNK_BYTES, IMAGE_EXTENSIONS,
    run_panes_websocket, terminal,
)
from herdr_web.input_transfer import (
    INPUT_TRANSFER_CHUNK_BYTES, InputTransfer, InputTransferReceiver,
)
from herdr_web.pane_stream import AnsiFrame, PaneController


def transfer_header(size, kind="clipboard-image", stream_id=1):
    return {"type": "input-transfer", "kind": kind, "size": size,
            "extension": "png", "stream_id": stream_id}


def make_transfer(header=None, **kwargs):
    return InputTransfer(header or transfer_header(4),
                         image_limit=MAX_CLIPBOARD_IMAGE_BYTES,
                         paste_limit=MAX_PANE_TEXT_PASTE_BYTES,
                         image_extensions=IMAGE_EXTENSIONS, pane_mode=True, **kwargs)


INITIAL_OUTPUT_CHUNKS = OUTPUT_ACK_WINDOW_BYTES // OUTPUT_WEBSOCKET_CHUNK_BYTES


async def wait_until(predicate):
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition did not become true")


class FakeWebSocket:
    headers = {}

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent_bytes = []
        self.sent_json = []
        self.closed = None

    async def accept(self):
        pass

    async def close(self, **kwargs):
        self.closed = kwargs

    async def receive_json(self):
        return {"type": "resize", "cols": 80, "rows": 24, "output_ack": True}

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, data):
        self.sent_json.append(data)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    def control(self, data):
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(data)})

    def binary(self, data):
        self.incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def disconnect(self):
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})


class FakeClient:
    master_fd = 1

    def __init__(self):
        self.closed = False
        self.resizes = []

    async def close(self):
        self.closed = True

    def resize(self, cols, rows):
        self.resizes.append((cols, rows))


class FakePane(PaneController):
    def __init__(self):
        self.records = asyncio.Queue()
        self.records.put_nowait(AnsiFrame(1, 80, 24, True, b"initial"))
        self.inputs = []
        self._closed = False

    async def read_record(self):
        return await self.records.get()

    async def send_input(self, data):
        self.inputs.append(data)

    async def resize(self, cols, rows):
        pass

    async def close(self):
        self._closed = True


class InputTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_chunks_and_utf8_boundary(self):
        transfer = make_transfer(transfer_header(4, "pane-paste"))
        self.assertFalse(transfer.feed(b"\xf0\x9f"))
        self.assertFalse(transfer.ready.is_set())
        self.assertTrue(transfer.feed(b"\x99\x82"))
        self.assertEqual(await transfer.command(),
                         ({"type": "pane-paste", "stream_id": 1, "text": "🙂"}, None))
        self.assertEqual(len(transfer.body), 0)

    async def test_invalid_sizes_kinds_and_chunk_bounds(self):
        for size in (True, False, 0, -1, 1.5, "4", MAX_CLIPBOARD_IMAGE_BYTES + 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                make_transfer(transfer_header(size))
        for header in (transfer_header(1, "unknown"),
                       transfer_header(1, []),
                       transfer_header(MAX_PANE_TEXT_PASTE_BYTES + 1, "pane-paste"),
                       transfer_header(1, stream_id=True),
                       {**transfer_header(1), "extension": "../png"}):
            with self.subTest(header=header), self.assertRaises(ValueError):
                make_transfer(header)
        for chunk in (b"", b"12345", b"x" * (INPUT_TRANSFER_CHUNK_BYTES + 1)):
            with self.subTest(length=len(chunk)), self.assertRaises(ValueError):
                make_transfer().feed(chunk)

    async def test_cancel_unblocks_without_partial_command(self):
        receiver = InputTransferReceiver()
        transfer = make_transfer()
        receiver.start(transfer)
        self.assertTrue(receiver.feed(b"12"))
        with self.assertRaises(ValueError):
            receiver.start(make_transfer())
        receiver.cancel()
        self.assertIsNone(await transfer.command())
        self.assertEqual(transfer.body, b"")
        self.assertFalse(receiver.feed(b"later key"))

    async def test_idle_deadline_does_not_reset_on_controls(self):
        receiver = InputTransferReceiver()
        with patch("herdr_web.input_transfer.INPUT_TRANSFER_IDLE_SECONDS", 0.01):
            receiver.start(make_transfer())
        websocket = FakeWebSocket()
        websocket.control({"type": "pong"})
        await receiver.receive(websocket)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            await receiver.receive(websocket)
        receiver.cancel()

    async def test_invalid_utf8_is_rejected_after_assembly(self):
        transfer = make_transfer(transfer_header(1, "pane-paste"))
        transfer.feed(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            await transfer.command()


class FullInputTests(unittest.IsolatedAsyncioTestCase):
    async def run_full(self, exercise, *, write=None, stage=None, queue_bytes=None):
        websocket, client = FakeWebSocket(), FakeClient()
        backend = Backend("backend", "test", Path("/unused.sock"))
        output = asyncio.Queue()
        output.put_nowait(b"x" * OUTPUT_ACK_WINDOW_BYTES * 8)
        async def read(_fd):
            return await output.get()
        patches = [
            patch("herdr_web.app.discover_backends", return_value={backend.id: backend}),
            patch("herdr_web.app.start_client", return_value=client),
            patch("herdr_web.app.read_pty_chunk", side_effect=read),
            patch("herdr_web.app.write_pty", side_effect=write or AsyncMock()),
            patch("herdr_web.app.PANE_COMMAND_DRAIN_TIMEOUT_SECONDS", 0.02),
        ]
        if stage:
            patches.append(patch("herdr_web.app.stage_clipboard_image_async", side_effect=stage))
        if queue_bytes is not None:
            patches.append(patch("herdr_web.app.PANE_COMMAND_QUEUE_BYTES", queue_bytes))
        for item in patches:
            item.start()
        task = asyncio.create_task(terminal(websocket, backend.id))
        try:
            await wait_until(lambda: len(websocket.sent_bytes) >= INITIAL_OUTPUT_CHUNKS)
            self.assertEqual(websocket.sent_json[0]["input_chunk_bytes"], 16 * 1024)
            await exercise(websocket, client, task)
        finally:
            websocket.disconnect()
            try:
                await asyncio.wait_for(task, 2)
            finally:
                for item in reversed(patches):
                    item.stop()
        self.assertTrue(client.closed)

    async def test_ack_intake_continues_during_blocked_write_in_order(self):
        started, release = asyncio.Event(), asyncio.Event()
        written = []

        async def write(_fd, data):
            if data == b"first":
                started.set()
                await release.wait()
            written.append(data)

        async def exercise(ws, client, task):
            ws.binary(b"first")
            await started.wait()
            ws.control({"type": "resize", "cols": 90, "rows": 25})
            ws.binary(b"second")
            ws.control({"type": "output-ack", "bytes": OUTPUT_ACK_WINDOW_BYTES})
            await wait_until(lambda: len(ws.sent_bytes) >= INITIAL_OUTPUT_CHUNKS * 2)
            self.assertEqual(written, [])
            self.assertEqual(client.resizes, [])
            release.set()
            await wait_until(lambda: len(written) == 2)
            self.assertEqual(written, [b"first", b"second"])
            self.assertEqual(client.resizes, [(90, 25)])

        await self.run_full(exercise, write=write)

    async def test_image_chunks_and_staging_never_block_ack(self):
        started, release = asyncio.Event(), asyncio.Event()
        staged, written = [], []

        async def stage(extension, data):
            staged.append((extension, data))
            started.set()
            await release.wait()
            raise ValueError("synthetic staging failure")

        async def write(_fd, data):
            written.append(data)

        async def exercise(ws, client, task):
            ws.control(transfer_header(4))
            ws.binary(b"12")
            ws.control({"type": "output-ack", "bytes": OUTPUT_ACK_WINDOW_BYTES})
            await wait_until(lambda: len(ws.sent_bytes) >= INITIAL_OUTPUT_CHUNKS * 2)
            self.assertEqual(staged, [])
            ws.control({"type": "resize", "cols": 90, "rows": 25})
            ws.binary(b"34")
            ws.binary(b"after image")
            await started.wait()
            ws.control({"type": "output-ack", "bytes": OUTPUT_ACK_WINDOW_BYTES * 2})
            await wait_until(lambda: len(ws.sent_bytes) >= INITIAL_OUTPUT_CHUNKS * 2 + 1)
            self.assertEqual(client.resizes, [])
            self.assertEqual(written, [])
            release.set()
            await wait_until(lambda: len(written) == 1)
            self.assertEqual(staged, [("png", b"1234")])
            self.assertEqual(client.resizes, [(90, 25)])
            self.assertEqual(written, [b"after image"])

        await self.run_full(exercise, write=write, stage=stage)

    async def test_cancel_and_disconnect_discard_partial_upload(self):
        written, staged = [], []

        async def write(_fd, data):
            written.append(data)

        async def stage(extension, data):
            staged.append(data)
            raise ValueError("must not stage")

        async def exercise(ws, client, task):
            ws.control(transfer_header(4))
            ws.binary(b"12")
            ws.control({"type": "input-transfer-cancel"})
            ws.binary(b"after cancel")
            await wait_until(lambda: written)
            ws.control(transfer_header(4))
            ws.binary(b"12")
            ws.disconnect()
            await task
            self.assertEqual(written, [b"after cancel"])
            self.assertEqual(staged, [])

        await self.run_full(exercise, write=write, stage=stage)

    async def test_declared_upload_bytes_remain_reserved_while_worker_waits(self):
        async def exercise(ws, client, task):
            ws.control(transfer_header(4))
            await asyncio.sleep(0.01)
            ws.control({"type": "resize", "cols": 90, "rows": 25})
            await task
            self.assertTrue(any("queue is full" in str(item) for item in ws.sent_json))
        await self.run_full(exercise, queue_bytes=4)

    async def test_bad_chunk_never_becomes_terminal_input(self):
        written = []
        async def write(_fd, data):
            written.append(data)
        async def exercise(ws, client, task):
            ws.control(transfer_header(4))
            ws.binary(b"oversized body")
            await task
            self.assertEqual(written, [])
            self.assertTrue(any("invalid input transfer chunk" in str(item)
                                for item in ws.sent_json))
        await self.run_full(exercise, write=write)

    async def test_disconnect_bounds_and_cancels_blocked_worker(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def write(_fd, data):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def exercise(ws, client, task):
            ws.binary(b"blocked")
            await started.wait()
            ws.disconnect()
            await asyncio.wait_for(task, 1)
            self.assertTrue(cancelled.is_set())
        await self.run_full(exercise, write=write)


class PaneInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_paste_keeps_target_and_keys_ordered_while_ack_is_live(self):
        ws, pane = FakeWebSocket(), FakePane()
        backend = Backend("backend", "test", Path("/unused.sock"))
        initial = {"type": "panes.attach", "tab_id": "t1",
                   "panes": [{"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}]}
        snapshot = {"tabs": [{"tab_id": "t1"}],
                    "panes": [{"pane_id": "p1", "tab_id": "t1"}]}
        calls = []
        async def api(_backend, method, params):
            calls.append((method, params))
            return {"result": {"type": "ok"}}
        with (
            patch("herdr_web.app.navigation_snapshot", AsyncMock(return_value=snapshot)),
            patch("herdr_web.app.start_pane_stream", AsyncMock(return_value=pane)),
            patch("herdr_web.app.run_herdr_socket_api", side_effect=api),
            patch("herdr_web.app.PANE_FRAME_ACK_TIMEOUT_SECONDS", 0.1),
        ):
            task = asyncio.create_task(run_panes_websocket(ws, backend, initial))
            try:
                await wait_until(lambda: ws.sent_bytes)
                ws.control(transfer_header(4, "pane-paste"))
                ws.binary(b"\xf0\x9f")
                ws.control({"type": "pane-output-ack", "stream_id": 1, "seq": "1"})
                # The short frame deadline would close the socket if the
                # command placeholder prevented parser-ACK processing.
                await asyncio.sleep(0.15)
                self.assertIsNone(ws.closed)
                self.assertEqual(calls, [])
                ws.binary(b"\x99\x82")
                ws.binary(b"after paste")
                await wait_until(lambda: pane.inputs)
                self.assertEqual(calls, [("pane.send_text", {"pane_id": "p1", "text": "🙂"})])
                self.assertEqual(pane.inputs, [b"after paste"])
            finally:
                ws.disconnect()
                await asyncio.wait_for(task, 2)
            self.assertTrue(pane.closed)

    async def test_external_cancellation_releases_partial_transfer_and_all_tasks(self):
        ws, pane = FakeWebSocket(), FakePane()
        backend = Backend("backend", "test", Path("/unused.sock"))
        initial = {"type": "panes.attach", "tab_id": "t1",
                   "panes": [{"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}]}
        snapshot = {"tabs": [{"tab_id": "t1"}],
                    "panes": [{"pane_id": "p1", "tab_id": "t1"}]}
        api = AsyncMock()
        before = asyncio.all_tasks()
        with (
            patch("herdr_web.app.navigation_snapshot", AsyncMock(return_value=snapshot)),
            patch("herdr_web.app.start_pane_stream", AsyncMock(return_value=pane)),
            patch("herdr_web.app.run_herdr_socket_api", api),
        ):
            task = asyncio.create_task(run_panes_websocket(ws, backend, initial))
            await wait_until(lambda: ws.sent_bytes)
            ws.control(transfer_header(4, "pane-paste"))
            ws.binary(b"12")
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(pane.closed)
        api.assert_not_called()
        self.assertEqual(pane.inputs, [])
        self.assertEqual(asyncio.all_tasks() - before, set())

    async def test_wrong_stream_never_delivers_paste_or_image(self):
        ws, pane = FakeWebSocket(), FakePane()
        backend = Backend("backend", "test", Path("/unused.sock"))
        initial = {"type": "panes.attach", "tab_id": "t1",
                   "panes": [{"stream_id": 1, "pane_id": "p1", "cols": 80, "rows": 24}]}
        snapshot = {"tabs": [{"tab_id": "t1"}],
                    "panes": [{"pane_id": "p1", "tab_id": "t1"}]}
        api = AsyncMock()
        with (
            patch("herdr_web.app.navigation_snapshot", AsyncMock(return_value=snapshot)),
            patch("herdr_web.app.start_pane_stream", AsyncMock(return_value=pane)),
            patch("herdr_web.app.run_herdr_socket_api", api),
        ):
            task = asyncio.create_task(run_panes_websocket(ws, backend, initial))
            await wait_until(lambda: ws.sent_bytes)
            ws.control(transfer_header(4, "pane-paste", stream_id=2))
            ws.binary(b"text")
            ws.control(transfer_header(4, stream_id=2))
            ws.binary(b"data")
            await asyncio.wait_for(task, 2)
        api.assert_not_called()
        self.assertEqual(pane.inputs, [])
        self.assertEqual(ws.closed["code"], 4400)


if __name__ == "__main__":
    unittest.main()
