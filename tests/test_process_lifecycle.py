import asyncio
import os
from pathlib import Path
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
