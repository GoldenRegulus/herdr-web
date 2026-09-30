import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from herdr_web.app import Backend, terminal
from herdr_web.output_flow import OUTPUT_CHUNK_BYTES, OUTPUT_WINDOW_INITIAL_BYTES


class FakeClient:
    master_fd = 1
    closed = False

    async def close(self):
        self.closed = True

    def resize(self, _cols, _rows):
        pass


class FakeWebSocket:
    headers = {}

    def __init__(self, ack_enabled=True):
        self.ack_enabled = ack_enabled
        self.incoming = asyncio.Queue()
        self.sent = []
        self.controls = []
        self.sent_event = asyncio.Event()

    async def accept(self):
        pass

    async def receive_json(self):
        return {'type': 'resize', 'cols': 80, 'rows': 24, 'output_ack': self.ack_enabled}

    async def receive(self):
        return await self.incoming.get()

    async def send_bytes(self, data):
        self.sent.append(data)
        self.sent_event.set()

    async def send_json(self, data):
        self.controls.append(data)

    async def close(self, **_kwargs):
        pass

    async def ack(self, value):
        await self.incoming.put({
            'type': 'websocket.receive',
            'text': json.dumps({'type': 'output-ack', 'bytes': value}),
        })

    async def wait_for_bytes(self, size):
        while sum(map(len, self.sent)) < size:
            self.sent_event.clear()
            await asyncio.wait_for(self.sent_event.wait(), timeout=1)


class FullOutputWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def run_connection(self, websocket, payload, *, idle=False, timeout=1):
        backend = Backend('test', 'test', Path('/unused.sock'))
        client = FakeClient()
        source = iter((payload, b''))

        async def read(_fd):
            data = next(source)
            if not data and idle:
                await asyncio.Event().wait()
            return data

        with (
            patch('herdr_web.app.discover_backends', return_value={backend.id: backend}),
            patch('herdr_web.app.start_client', return_value=client),
            patch('herdr_web.app.read_pty_chunk', side_effect=read),
            patch('herdr_web.app.OUTPUT_ACK_TIMEOUT_SECONDS', timeout),
        ):
            await terminal(websocket, backend.id)
        self.assertTrue(client.closed)

    async def test_invalid_acks_cannot_unlock_output(self):
        websocket = FakeWebSocket()
        payload = bytes(range(256)) * (OUTPUT_WINDOW_INITIAL_BYTES // 128)
        task = asyncio.create_task(self.run_connection(websocket, payload))
        self.addAsyncCleanup(self.cancel, task)
        await websocket.wait_for_bytes(OUTPUT_WINDOW_INITIAL_BYTES)
        for invalid in (True, False, -1, 0, 1.5, '32768', len(payload)):
            await websocket.ack(invalid)
        await asyncio.sleep(0.02)
        self.assertEqual(sum(map(len, websocket.sent)), OUTPUT_WINDOW_INITIAL_BYTES)
        await websocket.ack(OUTPUT_WINDOW_INITIAL_BYTES)
        await websocket.wait_for_bytes(len(payload))
        await websocket.ack(len(payload))
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(b''.join(websocket.sent), payload)

    async def test_no_ack_legacy_client_remains_compatible(self):
        websocket = FakeWebSocket(ack_enabled=False)
        payload = b'\x1b[?2026h\x1b[31m' + bytes(range(256)) * 2048 + b'\x1b[0m\x1b[?2026l'
        await asyncio.wait_for(self.run_connection(websocket, payload), timeout=1)
        self.assertEqual(b''.join(websocket.sent), payload)
        self.assertEqual(websocket.controls[0]['output_window_bytes'], 0)
        self.assertTrue(all(len(chunk) <= OUTPUT_CHUNK_BYTES for chunk in websocket.sent))

    async def test_immediate_parser_ack_cannot_race_sent_accounting(self):
        class ImmediateAckWebSocket(FakeWebSocket):
            async def send_bytes(self, data):
                await super().send_bytes(data)
                await self.ack(sum(map(len, self.sent)))
                await asyncio.sleep(0)

        websocket = ImmediateAckWebSocket()
        payload = b"\x1b[31m" + bytes(range(256)) * 2048 + b"\x1b[0m"
        await asyncio.wait_for(self.run_connection(websocket, payload), timeout=1)
        self.assertEqual(b"".join(websocket.sent), payload)
        self.assertFalse(any(control.get("type") == "error" for control in websocket.controls))

    async def test_idle_output_still_enforces_oldest_ack_deadline(self):
        websocket = FakeWebSocket()
        await asyncio.wait_for(
            self.run_connection(websocket, b'only one output', idle=True, timeout=0.03),
            timeout=1,
        )
        self.assertEqual(websocket.sent, [b'only one output'])
        self.assertEqual(websocket.controls[-1]['message'], 'terminal parser acknowledgement timed out')

    async def test_partial_progress_does_not_renew_oldest_deadline(self):
        websocket = FakeWebSocket()
        task = asyncio.create_task(self.run_connection(websocket, b'x' * OUTPUT_CHUNK_BYTES, timeout=0.08))
        self.addAsyncCleanup(self.cancel, task)
        await websocket.wait_for_bytes(OUTPUT_CHUNK_BYTES)
        # Keep sending advancing ACKs more frequently than the deadline. None
        # completes the oldest parser write, so the connection must still end.
        for value in range(1, 16):
            if task.done():
                break
            await websocket.ack(value)
            await asyncio.sleep(0.01)
        self.assertTrue(task.done(), 'partial ACKs renewed the oldest chunk deadline')
        await task
        self.assertEqual(websocket.controls[-1]['message'], 'terminal parser acknowledgement timed out')

    async def test_disconnect_while_window_full_cancels_wait(self):
        websocket = FakeWebSocket()
        task = asyncio.create_task(self.run_connection(websocket, b'x' * OUTPUT_WINDOW_INITIAL_BYTES * 2))
        self.addAsyncCleanup(self.cancel, task)
        await websocket.wait_for_bytes(OUTPUT_WINDOW_INITIAL_BYTES)
        await websocket.incoming.put({'type': 'websocket.disconnect', 'code': 1000})
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(sum(map(len, websocket.sent)), OUTPUT_WINDOW_INITIAL_BYTES)
        self.assertFalse(any(control.get('type') == 'error' for control in websocket.controls))

    async def cancel(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
