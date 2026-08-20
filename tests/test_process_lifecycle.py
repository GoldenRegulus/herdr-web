import asyncio
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from herdr_web.app import (
    Backend,
    read_pty_chunk,
    remove_stale_staged_images,
    schedule_staged_image_removal,
    staged_image_cleanup_tasks,
    start_client,
    start_named_backend,
    validate_session_name,
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
