"""Cross-feature regression for Full flow control, compression, and uploads."""

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zlib

from herdr_web.app import Backend, OUTPUT_ACK_WINDOW_BYTES, terminal
from herdr_web.full_output import FULL_OUTPUT_COMPRESSION


class FullTransportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_output_acks_continue_through_partial_upload_and_staging(self):
        incoming = asyncio.Queue()
        output = asyncio.Queue()
        initial_window = OUTPUT_ACK_WINDOW_BYTES
        payload = b"\x1b[32mterminal\x1b[0m\r\n" * (initial_window // 4)
        output.put_nowait(payload)
        controls, received, written = [], [], []
        staging_started, release_staging = asyncio.Event(), asyncio.Event()
        image_path = Path("/tmp/herdr-integration-image.png")

        class Client:
            master_fd = 1
            closed = False

            async def close(self):
                self.closed = True

        class Socket:
            headers = {}
            expected_length = None
            raw_sent = 0

            async def accept(self):
                pass

            async def receive_json(self):
                return {"type": "resize", "output_ack": True,
                        "output_compression": FULL_OUTPUT_COMPRESSION}

            async def receive(self):
                return await incoming.get()

            async def send_json(self, message):
                controls.append(message)
                if message.get("type") == "output-deflate":
                    self.expected_length = message["bytes"]

            async def send_bytes(self, data):
                raw = zlib.decompress(data) if self.expected_length is not None else data
                if self.expected_length is not None:
                    assert len(raw) == self.expected_length
                self.expected_length = None
                received.append(raw)
                self.raw_sent += len(raw)

            async def close(self, **_options):
                pass

        def control(message):
            incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

        def binary(data):
            incoming.put_nowait({"type": "websocket.receive", "bytes": data})

        async def wait_for(predicate):
            async def poll():
                while not predicate():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(poll(), 1)

        async def read(_fd):
            return await output.get()

        async def stage(extension, data):
            self.assertEqual((extension, data), ("png", b"1234"))
            staging_started.set()
            await release_staging.wait()
            return image_path

        async def write(_fd, data):
            written.append(data)

        backend = Backend("backend", "test", Path("/unused.sock"))
        socket, client = Socket(), Client()
        with (
            patch("herdr_web.app.discover_backends", return_value={backend.id: backend}),
            patch("herdr_web.app.start_client", return_value=client),
            patch("herdr_web.app.read_pty_chunk", side_effect=read),
            patch("herdr_web.app.stage_clipboard_image_async", side_effect=stage),
            patch("herdr_web.app.write_pty", side_effect=write),
            patch("herdr_web.app.schedule_staged_image_removal") as remove_later,
        ):
            task = asyncio.create_task(terminal(socket, backend.id))
            try:
                await wait_for(lambda: socket.raw_sent == initial_window)
                self.assertEqual(controls[0]["output_compression"], FULL_OUTPUT_COMPRESSION)
                self.assertEqual(controls[0]["input_chunk_bytes"], 16 * 1024)
                control({"type": "input-transfer", "kind": "clipboard-image",
                         "extension": "png", "size": 4})
                binary(b"12")
                control({"type": "output-ack", "bytes": initial_window})
                await wait_for(lambda: socket.raw_sent > initial_window)
                self.assertFalse(staging_started.is_set())

                binary(b"34")
                binary(b"key after image")
                await asyncio.wait_for(staging_started.wait(), 1)
                sent_before_ack = socket.raw_sent
                control({"type": "output-ack", "bytes": sent_before_ack})
                await wait_for(lambda: socket.raw_sent > sent_before_ack)
                self.assertEqual(written, [])
                release_staging.set()
                await wait_for(lambda: len(written) == 2)
                self.assertEqual(written, [
                    b"\x1b[200~" + str(image_path).encode() + b"\x1b[201~",
                    b"key after image",
                ])
                while socket.raw_sent < len(payload):
                    sent = socket.raw_sent
                    control({"type": "output-ack", "bytes": sent})
                    await wait_for(lambda: socket.raw_sent > sent)
                control({"type": "output-ack", "bytes": len(payload)})
                output.put_nowait(b"")
                await asyncio.wait_for(task, 1)
                self.assertEqual(b"".join(received), payload)
                remove_later.assert_called_once_with(image_path)
                self.assertTrue(client.closed)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
