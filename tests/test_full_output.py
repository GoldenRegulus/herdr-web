import asyncio
import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch
import zlib

from herdr_web.app import Backend, OUTPUT_WEBSOCKET_CHUNK_BYTES, terminal
from herdr_web.output_flow import OUTPUT_WINDOW_INITIAL_BYTES
from herdr_web.full_output import (
    FULL_OUTPUT_COMPRESSION,
    FULL_OUTPUT_CPU_BUDGET_SECONDS,
    FULL_OUTPUT_MAX_BYTES,
    FULL_OUTPUT_POOR_SAVINGS_SKIP_CHUNKS,
    FullOutputEncoder,
    websocket_message_size,
)


class FullOutputEncoderTests(unittest.TestCase):
    def test_flow_and_compression_chunk_limits_match(self):
        self.assertEqual(OUTPUT_WEBSOCKET_CHUNK_BYTES, FULL_OUTPUT_MAX_BYTES)

    def test_compresses_independent_bounded_chunks_with_real_wire_savings(self):
        encoder = FullOutputEncoder()
        for raw in (b"\x1b[31mterminal cells\x1b[0m\r\n" * 250, b"x" * 8192):
            packet = encoder.encode(raw)
            self.assertEqual(packet.descriptor, {"type": "output-deflate", "bytes": len(raw)})
            self.assertEqual(zlib.decompress(packet.payload), raw)
            descriptor = json.dumps(packet.descriptor, separators=(",", ":")).encode()
            self.assertLess(
                websocket_message_size(len(descriptor)) + websocket_message_size(len(packet.payload)),
                websocket_message_size(len(raw)),
            )

    def test_tiny_and_incompressible_chunks_are_unchanged(self):
        encoder = FullOutputEncoder()
        for raw in (b"$ ", random.Random(91).randbytes(8192)):
            packet = encoder.encode(raw)
            self.assertIs(packet.payload, raw)
            self.assertIsNone(packet.descriptor)

    def test_marginal_compression_does_not_pay_descriptor_overhead(self):
        raw = random.Random(21).randbytes(700) + b"x" * 100
        self.assertLess(len(zlib.compress(raw, 1)), len(raw))
        self.assertIsNone(FullOutputEncoder().encode(raw).descriptor)

    def test_rejects_out_of_bounds_compression_work(self):
        for raw in (b"", b"x" * (FULL_OUTPUT_MAX_BYTES + 1)):
            with self.assertRaises(ValueError):
                FullOutputEncoder().encode(raw)

    def test_incompressible_chunks_back_off_before_retrying(self):
        encoder = FullOutputEncoder()
        raw = random.Random(13).randbytes(8192)
        with patch("herdr_web.full_output.zlib.compress", wraps=zlib.compress) as compress:
            encoder.encode(raw)
            for _ in range(FULL_OUTPUT_POOR_SAVINGS_SKIP_CHUNKS):
                self.assertIsNone(encoder.encode(b"x" * 8192).descriptor)
            self.assertEqual(compress.call_count, 1)
            self.assertIsNotNone(encoder.encode(b"x" * 8192).descriptor)
            self.assertEqual(compress.call_count, 2)

    def test_expensive_compression_backs_off_for_one_second(self):
        encoder = FullOutputEncoder()
        raw = b"x" * 8192
        slow = FULL_OUTPUT_CPU_BUDGET_SECONDS * 2
        with patch("herdr_web.full_output.time.monotonic", side_effect=[10, 10 + slow, 10.5, 12, 12.001]):
            self.assertIsNotNone(encoder.encode(raw).descriptor)
            self.assertIsNone(encoder.encode(raw).descriptor)
            self.assertIsNotNone(encoder.encode(raw).descriptor)


class FakeClient:
    master_fd = 1
    closed = False

    async def close(self):
        self.closed = True


class FakeWebSocket:
    headers = {}

    def __init__(self, compression, acknowledge=True, auto_ack=True):
        self.compression = compression
        self.acknowledge = acknowledge
        self.auto_ack = auto_ack
        self.raw_sent = 0
        self.next_raw_size = None
        self.incoming = asyncio.Queue()
        self.messages = []
        self.sent_bytes = []

    async def accept(self):
        pass

    async def receive_json(self):
        return {"type": "resize", "output_ack": self.acknowledge, "output_compression": self.compression}

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, message):
        self.messages.append(message)
        if message.get("type") == "output-deflate":
            self.next_raw_size = message["bytes"]

    async def send_bytes(self, data):
        self.messages.append(data)
        self.sent_bytes.append(data)
        self.raw_sent += self.next_raw_size if self.next_raw_size is not None else len(data)
        self.next_raw_size = None
        if self.auto_ack and self.acknowledge:
            await self.ack(self.raw_sent)

    async def close(self, **options):
        pass

    async def ack(self, size):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "output-ack", "bytes": size})})


class FullOutputWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def run_socket(self, websocket, payload, inspect=None):
        backend = Backend("backend", "test", Path("/unused.sock"))
        client = FakeClient()
        output = iter((payload, b""))

        async def read(_fd):
            return next(output)

        with (
            patch("herdr_web.app.discover_backends", return_value={backend.id: backend}),
            patch("herdr_web.app.start_client", return_value=client),
            patch("herdr_web.app.read_pty_chunk", side_effect=read),
        ):
            task = asyncio.create_task(terminal(websocket, backend.id))
            try:
                if inspect is not None:
                    await inspect(websocket, task, client)
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(client.closed)

    async def wait_chunks(self, socket, count):
        async def wait():
            while len(socket.sent_bytes) < count:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait(), 1)

    async def test_negotiated_compression_acks_original_parser_bytes(self):
        initial_chunks = OUTPUT_WINDOW_INITIAL_BYTES // FULL_OUTPUT_MAX_BYTES
        payload = b"x" * (OUTPUT_WINDOW_INITIAL_BYTES + 8192 + 37)
        socket = FakeWebSocket(FULL_OUTPUT_COMPRESSION, auto_ack=False)

        async def inspect(socket, task, client):
            await self.wait_chunks(socket, initial_chunks)
            self.assertLess(len(socket.sent_bytes[0]), 8192)
            await socket.ack(len(socket.sent_bytes[0]))
            await asyncio.sleep(0.02)
            self.assertEqual(len(socket.sent_bytes), initial_chunks)
            await socket.ack(8192)
            await self.wait_chunks(socket, initial_chunks + 1)
            await socket.ack(OUTPUT_WINDOW_INITIAL_BYTES + 8192)
            await self.wait_chunks(socket, initial_chunks + 2)
            await socket.ack(len(payload))

        await self.run_socket(socket, payload, inspect)
        self.assertEqual(socket.messages[0]["output_compression"], FULL_OUTPUT_COMPRESSION)
        for index in range(initial_chunks + 1):
            self.assertEqual(socket.messages[1 + index * 2], {"type": "output-deflate", "bytes": 8192})
        self.assertEqual(b"".join(zlib.decompress(part) for part in socket.sent_bytes[:-1]) + socket.sent_bytes[-1], payload)
        self.assertEqual(socket.messages[-1], b"x" * 37)

    async def test_final_compressed_output_waits_for_parser_ack_at_eof(self):
        socket = FakeWebSocket(FULL_OUTPUT_COMPRESSION, auto_ack=False)

        async def inspect(socket, task, client):
            await self.wait_chunks(socket, 1)
            # EOF has already been read, but a browser can still be decoding.
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.assertFalse(client.closed)
            await socket.ack(len(socket.sent_bytes[0]))
            await asyncio.sleep(0.02)
            self.assertFalse(task.done(), "compressed wire bytes must not release the final ACK drain")
            await socket.ack(8192)

        await self.run_socket(socket, b"x" * 8192, inspect)

    async def test_missing_final_compressed_ack_has_a_finite_deadline(self):
        socket = FakeWebSocket(FULL_OUTPUT_COMPRESSION, auto_ack=False)
        with patch("herdr_web.app.OUTPUT_ACK_TIMEOUT_SECONDS", 0.02):
            await self.run_socket(socket, b"x" * 8192)
        self.assertEqual(socket.messages[-1], {"type": "error", "message": "terminal parser acknowledgement timed out"})

    async def test_compression_without_parser_ack_support_stays_raw(self):
        socket = FakeWebSocket(FULL_OUTPUT_COMPRESSION, acknowledge=False)
        await self.run_socket(socket, b"x" * 8192)
        self.assertIsNone(socket.messages[0]["output_compression"])
        self.assertEqual(socket.messages[1:], [b"x" * 8192])

    async def test_legacy_and_unknown_compression_keep_raw_output(self):
        payload = b"x" * 8192
        for compression in (None, "deflate", True, "future-codec"):
            with self.subTest(compression=compression):
                socket = FakeWebSocket(compression)
                await self.run_socket(socket, payload)
                self.assertIsNone(socket.messages[0]["output_compression"])
                self.assertEqual(socket.messages[1:], [payload])

    async def test_negotiated_incompressible_output_needs_no_descriptor(self):
        payload = random.Random(71).randbytes(8192)
        socket = FakeWebSocket(FULL_OUTPUT_COMPRESSION)
        await self.run_socket(socket, payload)
        self.assertEqual(socket.messages[1:], [payload])

    async def test_descriptor_payload_pair_does_not_interleave_queued_errors(self):
        socket = FakeWebSocket(FULL_OUTPUT_COMPRESSION)
        send_json = socket.send_json

        async def send_with_input(message):
            await send_json(message)
            if message.get("type") == "output-deflate":
                await socket.incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "clipboard-image", "extension": "invalid", "size": 1})})
                await asyncio.sleep(0.01)

        socket.send_json = send_with_input
        await self.run_socket(socket, b"x" * 8192)
        self.assertEqual(socket.messages[1]["type"], "output-deflate")
        self.assertIsInstance(socket.messages[2], bytes)

    async def test_compression_send_timeout_still_closes_client(self):
        socket = FakeWebSocket(FULL_OUTPUT_COMPRESSION)

        async def block(_data):
            await asyncio.Future()

        socket.send_bytes = block
        with patch("herdr_web.app.WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.02):
            await self.run_socket(socket, b"x" * 8192)
        self.assertEqual(socket.messages[-1], {"type": "error", "message": "terminal WebSocket send timed out"})
