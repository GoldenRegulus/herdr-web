import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from herdr_web.app import (
    Backend,
    BrowserSession,
    OUTPUT_ACK_WINDOW_BYTES,
    OUTPUT_WEBSOCKET_CHUNK_BYTES,
    read_pty,
    read_pty_chunk,
    remove_stale_staged_images,
    schedule_staged_image_removal,
    staged_image_cleanup_tasks,
    start_client,
    start_named_backend,
    terminal as terminal_websocket,
    validate_session_name,
    websocket_output_chunks,
    write_pty,
)


class PtyClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, script: str):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        binary = Path(temporary_directory.name) / "fake-herdr"
        binary.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        binary.chmod(0o700)
        environment = patch.dict(os.environ, {"HERDR_BINARY": str(binary)})
        environment.start()
        self.addCleanup(environment.stop)
        return start_client(Backend("test", "test", Path("/unused.sock")), 80, 24)

    async def test_pty_round_trip(self) -> None:
        client = self.make_client("stty raw -echo\ncat")
        self.addAsyncCleanup(client.close)

        await asyncio.sleep(0.05)
        await write_pty(client.master_fd, b"round trip")
        output = await asyncio.wait_for(read_pty_chunk(client.master_fd), timeout=1)

        self.assertEqual(output, b"round trip")

    def test_output_chunks_fit_the_acknowledgement_window(self) -> None:
        payload = b"x" * (OUTPUT_WEBSOCKET_CHUNK_BYTES * 2 + 37)
        chunks = list(websocket_output_chunks(payload))

        self.assertEqual(b"".join(chunks), payload)
        self.assertEqual(
            [len(chunk) for chunk in chunks],
            [OUTPUT_WEBSOCKET_CHUNK_BYTES, OUTPUT_WEBSOCKET_CHUNK_BYTES, 37],
        )
        self.assertEqual(OUTPUT_ACK_WINDOW_BYTES, OUTPUT_WEBSOCKET_CHUNK_BYTES)

    async def test_http_output_queue_uses_acknowledgement_sized_chunks(self) -> None:
        payload = b"x" * (OUTPUT_WEBSOCKET_CHUNK_BYTES * 2 + 37)

        class FakeClient:
            master_fd = 1

        session = BrowserSession(client=FakeClient())
        output = iter((payload, b""))

        async def fake_read_pty_chunk(_fd: int) -> bytes:
            return next(output)

        with patch(
            "herdr_web.app.read_pty_chunk", side_effect=fake_read_pty_chunk
        ):
            reader = asyncio.create_task(read_pty(session))
            chunks = [
                await asyncio.wait_for(session.output.get(), timeout=1)
                for _ in range(3)
            ]
            await asyncio.wait_for(reader, timeout=1)

        self.assertTrue(session.closed)
        self.assertEqual(b"".join(chunks), payload)
        self.assertTrue(
            all(len(chunk) <= OUTPUT_WEBSOCKET_CHUNK_BYTES for chunk in chunks)
        )

    async def test_full_websocket_waits_for_each_output_chunk_ack(self) -> None:
        payload = b"x" * (OUTPUT_WEBSOCKET_CHUNK_BYTES * 2 + 37)
        backend = Backend("backend", "test", Path("/unused.sock"))

        class FakeClient:
            master_fd = 1

            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

            def resize(self, _cols: int, _rows: int) -> None:
                pass

        class FakeWebSocket:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.sent_bytes: list[bytes] = []
                self.sent_json: list[dict[str, object]] = []
                self.accepted = False

            async def accept(self) -> None:
                self.accepted = True

            async def receive_json(self) -> dict[str, object]:
                return {
                    "type": "resize",
                    "cols": 80,
                    "rows": 24,
                    "output_ack": True,
                }

            async def receive(self) -> dict[str, object]:
                return await self.incoming.get()

            async def send_bytes(self, data: bytes) -> None:
                self.sent_bytes.append(data)

            async def send_json(self, data: dict[str, object]) -> None:
                self.sent_json.append(data)

            async def close(self, **_options: object) -> None:
                pass

        websocket = FakeWebSocket()
        client = FakeClient()
        output = iter((payload, b""))

        async def fake_read_pty_chunk(_fd: int) -> bytes:
            return next(output)

        async def wait_for_chunks(count: int) -> None:
            for _ in range(200):
                if len(websocket.sent_bytes) >= count:
                    return
                await asyncio.sleep(0.005)
            self.fail(f"timed out waiting for {count} output chunks")

        with (
            patch("herdr_web.app.discover_backends", return_value={backend.id: backend}),
            patch("herdr_web.app.start_client", return_value=client),
            patch("herdr_web.app.read_pty_chunk", side_effect=fake_read_pty_chunk),
        ):
            task = asyncio.create_task(terminal_websocket(websocket, backend.id))
            await wait_for_chunks(1)
            await asyncio.sleep(0.03)
            self.assertEqual(len(websocket.sent_bytes), 1)
            await websocket.incoming.put(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "type": "clipboard-image",
                            "extension": "invalid",
                            "size": 1,
                        }
                    ),
                }
            )

            acknowledged = 0
            for count in (2, 3):
                acknowledged += len(websocket.sent_bytes[-1])
                await websocket.incoming.put(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {"type": "output-ack", "bytes": acknowledged}
                        ),
                    }
                )
                await wait_for_chunks(count)

            acknowledged += len(websocket.sent_bytes[-1])
            await websocket.incoming.put(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {"type": "output-ack", "bytes": acknowledged}
                    ),
                }
            )
            await asyncio.wait_for(task, timeout=2)

        self.assertTrue(websocket.accepted)
        self.assertTrue(client.closed)
        self.assertTrue(
            any(message.get("type") == "error" for message in websocket.sent_json)
        )
        self.assertEqual(b"".join(websocket.sent_bytes), payload)
        self.assertEqual(
            [len(chunk) for chunk in websocket.sent_bytes],
            [OUTPUT_WEBSOCKET_CHUNK_BYTES, OUTPUT_WEBSOCKET_CHUNK_BYTES, 37],
        )

    async def test_full_websocket_parser_ack_timeout_releases_client(self) -> None:
        payload = b"x" * (OUTPUT_WEBSOCKET_CHUNK_BYTES * 2)
        backend = Backend("backend", "test", Path("/unused.sock"))

        class FakeClient:
            master_fd = 1

            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

            def resize(self, _cols: int, _rows: int) -> None:
                pass

        class FakeWebSocket:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.sent_bytes: list[bytes] = []
                self.sent_json: list[dict[str, object]] = []

            async def accept(self) -> None:
                pass

            async def receive_json(self) -> dict[str, object]:
                return {
                    "type": "resize",
                    "cols": 80,
                    "rows": 24,
                    "output_ack": True,
                }

            async def receive(self) -> dict[str, object]:
                return await self.incoming.get()

            async def send_bytes(self, data: bytes) -> None:
                self.sent_bytes.append(data)

            async def send_json(self, data: dict[str, object]) -> None:
                self.sent_json.append(data)

            async def close(self, **_options: object) -> None:
                pass

        websocket = FakeWebSocket()
        client = FakeClient()
        output = iter((payload, b""))

        async def fake_read_pty_chunk(_fd: int) -> bytes:
            return next(output)

        with (
            patch("herdr_web.app.discover_backends", return_value={backend.id: backend}),
            patch("herdr_web.app.start_client", return_value=client),
            patch("herdr_web.app.read_pty_chunk", side_effect=fake_read_pty_chunk),
            patch("herdr_web.app.OUTPUT_ACK_TIMEOUT_SECONDS", 0.02),
        ):
            await asyncio.wait_for(
                terminal_websocket(websocket, backend.id), timeout=2
            )

        self.assertTrue(client.closed)
        self.assertEqual(len(websocket.sent_bytes), 1)
        self.assertTrue(
            any(
                message.get("message") == "terminal parser acknowledgement timed out"
                for message in websocket.sent_json
            )
        )

    async def test_full_websocket_blocked_output_and_error_cleanup_client(self) -> None:
        backend = Backend("backend", "test", Path("/unused.sock"))

        class FakeClient:
            master_fd = 1

            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

            def resize(self, _cols: int, _rows: int) -> None:
                pass

        class BlockedWebSocket:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.output_send_cancelled = asyncio.Event()
                self.error_send_started = asyncio.Event()
                self.error_send_cancelled = asyncio.Event()

            async def accept(self) -> None:
                pass

            async def receive_json(self) -> dict[str, object]:
                return {
                    "type": "resize",
                    "cols": 80,
                    "rows": 24,
                    "output_ack": True,
                }

            async def receive(self) -> dict[str, object]:
                await asyncio.Event().wait()

            async def send_bytes(self, _data: bytes) -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.output_send_cancelled.set()

            async def send_json(self, data: dict[str, object]) -> None:
                if data.get("type") == "attached":
                    return
                self.error_send_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.error_send_cancelled.set()

            async def close(self, **_options: object) -> None:
                pass

        websocket = BlockedWebSocket()
        client = FakeClient()
        output = iter((b"blocked output", b""))

        async def fake_read_pty_chunk(_fd: int) -> bytes:
            return next(output)

        with (
            patch("herdr_web.app.discover_backends", return_value={backend.id: backend}),
            patch("herdr_web.app.start_client", return_value=client),
            patch("herdr_web.app.read_pty_chunk", side_effect=fake_read_pty_chunk),
            patch("herdr_web.app.WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.02),
        ):
            await asyncio.wait_for(terminal_websocket(websocket, backend.id), timeout=1)

        self.assertTrue(websocket.output_send_cancelled.is_set())
        self.assertTrue(websocket.error_send_started.is_set())
        self.assertTrue(websocket.error_send_cancelled.is_set())
        self.assertTrue(client.closed)

    async def test_full_websocket_blocked_attached_message_closes_client(self) -> None:
        backend = Backend("backend", "test", Path("/unused.sock"))

        class FakeClient:
            master_fd = 1

            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

            def resize(self, _cols: int, _rows: int) -> None:
                pass

        class BlockedWebSocket:
            headers: dict[str, str] = {}

            def __init__(self) -> None:
                self.attached_send_cancelled = asyncio.Event()

            async def accept(self) -> None:
                pass

            async def receive_json(self) -> dict[str, object]:
                return {"type": "resize", "cols": 80, "rows": 24}

            async def send_json(self, _data: dict[str, object]) -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.attached_send_cancelled.set()

            async def close(self, **_options: object) -> None:
                pass

        websocket = BlockedWebSocket()
        client = FakeClient()
        with (
            patch("herdr_web.app.discover_backends", return_value={backend.id: backend}),
            patch("herdr_web.app.start_client", return_value=client),
            patch("herdr_web.app.WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.02),
        ):
            await asyncio.wait_for(terminal_websocket(websocket, backend.id), timeout=1)

        self.assertTrue(websocket.attached_send_cancelled.is_set())
        self.assertTrue(client.closed)

    async def test_read_coalesces_a_short_output_burst(self) -> None:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        self.addCleanup(os.close, read_fd)
        os.write(write_fd, b"first")

        async def finish_burst() -> None:
            await asyncio.sleep(0.005)
            os.write(write_fd, b" second")
            os.close(write_fd)

        writer = asyncio.create_task(finish_burst())
        with patch("herdr_web.app.PTY_COALESCE_SECONDS", 0.05):
            output = await asyncio.wait_for(read_pty_chunk(read_fd), timeout=1)
        await writer

        self.assertEqual(output, b"first second")

    async def test_staged_image_cleanup_survives_shutdown_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            stale = directory / "image-stale.png"
            unrelated = directory / "other-file.png"
            stale.write_bytes(b"stale")
            unrelated.write_bytes(b"keep")
            with patch("herdr_web.app.STAGED_IMAGE_DIRECTORY", directory):
                remove_stale_staged_images()
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())

            staged = directory / "image-current.png"
            staged.write_bytes(b"current")
            previous_tasks = set(staged_image_cleanup_tasks)
            schedule_staged_image_removal(staged)
            task = next(iter(staged_image_cleanup_tasks - previous_tasks))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with patch("herdr_web.app.STAGED_IMAGE_DIRECTORY", directory):
                remove_stale_staged_images()
            self.assertFalse(staged.exists())

    async def test_start_named_backend_detaches_launcher(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        config = root / "config"
        binary = root / "fake-herdr"
        binary.write_text(
            """#!/usr/bin/env python3
import os
from pathlib import Path
import socket
import sys
import time

name = sys.argv[2]
root = Path(os.environ["HERDR_WEB_CONFIG_DIR"])
session = root / "sessions" / name
session.mkdir(parents=True, exist_ok=True)
(root / "parent-environment").write_text(os.environ.get("HERDR_ENV", ""))
pid = os.fork()
if pid:
    (root / "launcher-pid").write_text(str(os.getpid()))
    (root / "daemon-pid").write_text(str(pid))
    while True:
        time.sleep(1)
devnull = os.open(os.devnull, os.O_RDWR)
os.dup2(devnull, 0)
os.dup2(devnull, 1)
os.dup2(devnull, 2)
sock = socket.socket(socket.AF_UNIX)
sock.bind(str(session / "herdr-client.sock"))
sock.listen()
while True:
    connection, _ = sock.accept()
    connection.close()
""",
            encoding="utf-8",
        )
        binary.chmod(0o700)
        environment = patch.dict(
            os.environ,
            {
                "HERDR_BINARY": str(binary),
                "HERDR_WEB_CONFIG_DIR": str(config),
                "HERDR_ENV": "must-not-leak",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

        backend = await asyncio.wait_for(start_named_backend("test-session"), timeout=3)
        daemon_pid = int((config / "daemon-pid").read_text())

        def stop_daemon() -> None:
            try:
                os.kill(daemon_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(stop_daemon)

        self.assertEqual(backend.label, "test-session")
        self.assertEqual((config / "parent-environment").read_text(), "")
        launcher_pid = int((config / "launcher-pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(launcher_pid, 0)

    def test_validate_session_name(self) -> None:
        self.assertEqual(validate_session_name("Agent_1.test-name"), "Agent_1.test-name")
        for invalid in ("", ".", "..", "has space", "slash/name", "café", "a" * 65):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_session_name(invalid)

    async def test_close_reaps_process_group(self) -> None:
        client = self.make_client("sleep 300 &\nwait")
        pid = client.pid
        await asyncio.sleep(0.05)

        await client.close()

        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)


if __name__ == "__main__":
    unittest.main()
