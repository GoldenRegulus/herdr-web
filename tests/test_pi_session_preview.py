import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from herdr_web.app import (
    PiSessionPreviewError,
    find_pi_session_by_id,
    find_recent_pi_session,
    pi_message_response,
    pi_pane_mapping_path,
    pi_session_from_snapshot,
    pi_stream_path,
    read_pi_session_messages,
    read_pi_stream,
    read_pi_pane_mapping,
    project_markdown_files,
    read_project_markdown_file,
    read_scratchpad,
    scratchpad_path,
    write_scratchpad,
)


class PiSessionPreviewTests(unittest.TestCase):
    def write_session(self, directory: str, records: list[dict]) -> Path:
        path = Path(directory) / "session.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    def test_live_overlay_does_not_mutate_cached_messages(self) -> None:
        persisted = {
            "session_id": "session",
            "cwd": "/project",
            "messages": [{"id": "a1", "role": "assistant", "markdown": "done"}],
        }
        stream = {
            "id": "streaming-assistant", "role": "assistant",
            "markdown": "partial", "streaming": True,
        }
        with (
            patch("herdr_web.app.read_pi_session_messages", return_value=persisted),
            patch("herdr_web.app.read_pi_stream", return_value=stream),
        ):
            first = pi_message_response(Path("/session.jsonl"), 10)
            second = pi_message_response(Path("/session.jsonl"), 10)

        self.assertEqual([message["id"] for message in first["messages"]], ["a1", "streaming-assistant"])
        self.assertEqual([message["id"] for message in second["messages"]], ["a1", "streaming-assistant"])
        self.assertEqual([message["id"] for message in persisted["messages"]], ["a1"])

    def test_lists_and_reads_project_markdown_without_exposing_root(self) -> None:
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            (root / "docs").mkdir()
            (root / ".git").mkdir()
            (root / "README.md").write_text("# Read me", encoding="utf-8")
            (root / "docs" / "plan.markdown").write_text("## Plan", encoding="utf-8")
            (root / ".git" / "private.md").write_text("secret", encoding="utf-8")
            (root / "notes.txt").write_text("not markdown", encoding="utf-8")

            files = project_markdown_files(project)

            self.assertEqual([item["name"] for item in files], ["README.md", "plan.markdown"])
            self.assertEqual([item["location"] for item in files], ["", "docs"])
            self.assertNotIn(project, json.dumps(files))
            plan = next(item for item in files if item["name"] == "plan.markdown")
            document = read_project_markdown_file(project, plan["id"])
            self.assertEqual(document["markdown"], "## Plan")
            self.assertEqual(document["location"], "docs")

    def test_markdown_file_ids_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as project:
            with self.assertRaises(PiSessionPreviewError) as raised:
                read_project_markdown_file(project, "../../private")
            self.assertEqual(raised.exception.status_code, 404)

    def test_scratchpad_is_private_and_project_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as config:
            with patch.dict(os.environ, {"HERDR_WEB_CONFIG_DIR": config}):
                first = scratchpad_path("/project/one")
                second = scratchpad_path("/project/two")
                self.assertNotEqual(first, second)
                self.assertEqual(first.parent, Path(config) / "scratchpads")
                self.assertNotIn("project", first.name)

                write_scratchpad("/project/one", "# Notes\n\n**rich**")
                self.assertEqual(read_scratchpad("/project/one"), "# Notes\n\n**rich**")
                self.assertEqual(read_scratchpad("/project/two"), "")
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)
                self.assertEqual(first.parent.stat().st_mode & 0o777, 0o700)

    def test_scratchpad_rejects_oversize_content(self) -> None:
        with tempfile.TemporaryDirectory() as config:
            with (
                patch.dict(os.environ, {"HERDR_WEB_CONFIG_DIR": config}),
                patch("herdr_web.app.MAX_SCRATCHPAD_BYTES", 4),
            ):
                with self.assertRaises(PiSessionPreviewError) as raised:
                    write_scratchpad("/project", "12345")
                self.assertEqual(raised.exception.status_code, 413)

    def test_reads_latest_messages_from_active_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_session(directory, [
                {"type": "session", "version": 3, "id": "session", "cwd": "/project"},
                {"type": "message", "id": "u1", "parentId": None, "message": {"role": "user", "content": "first"}},
                {"type": "message", "id": "a1", "parentId": "u1", "message": {"role": "assistant", "content": [{"type": "text", "text": "old branch"}]}},
                {"type": "message", "id": "u2", "parentId": "u1", "message": {"role": "user", "content": [{"type": "text", "text": "new branch"}, {"type": "image", "data": "private"}]}},
                {"type": "message", "id": "a2", "parentId": "u2", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "answer"}]}},
            ])

            result = read_pi_session_messages(path)

            self.assertEqual(result["session_id"], "session")
            self.assertEqual(result["session_bytes"], path.stat().st_size)
            self.assertEqual(
                [message["markdown"] for message in result["messages"]],
                ["first", "new branch\n\n[Image]", "answer"],
            )
            self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("private", json.dumps(result))

    def test_limits_messages_and_surfaces_assistant_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            records = [{"type": "session", "id": "session"}]
            parent = None
            for index in range(12):
                entry_id = f"m{index}"
                message = {"role": "user", "content": str(index)}
                if index == 11:
                    message = {"role": "assistant", "content": [], "errorMessage": "failed"}
                records.append({"type": "message", "id": entry_id, "parentId": parent, "message": message})
                parent = entry_id
            path = self.write_session(directory, records)

            result = read_pi_session_messages(path, 3)

            self.assertEqual(len(result["messages"]), 3)
            self.assertEqual(result["messages"][-1]["markdown"], "**Error:** failed")

    def test_resolves_only_pi_agent_session_for_selected_pane(self) -> None:
        snapshot = {
            "focused_pane_id": "p1",
            "agents": [{
                "pane_id": "p1",
                "agent": "pi",
                "agent_session": {"value": "/tmp/example.jsonl"},
            }],
        }
        self.assertEqual(pi_session_from_snapshot(snapshot, None), Path("/tmp/example.jsonl"))
        snapshot["agents"][0]["agent_session"] = None
        self.assertIsNone(pi_session_from_snapshot(snapshot, "p1"))
        snapshot["agents"][0]["agent"] = "other"
        with self.assertRaisesRegex(RuntimeError, "not running pi"):
            pi_session_from_snapshot(snapshot, "p1")

    def test_oversize_session_reports_explicit_payload_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.jsonl"
            path.write_text('{"type":"session","id":"s"}\n', encoding="utf-8")
            with patch("herdr_web.app.MAX_PI_SESSION_BYTES", 1):
                with self.assertRaises(PiSessionPreviewError) as raised:
                    read_pi_session_messages(path)

            self.assertEqual(raised.exception.status_code, 413)
            self.assertIn("too large to preview", str(raised.exception))
            self.assertIn("limit 0 MiB", str(raised.exception))

    def test_finds_session_by_opaque_id_across_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            session_id = "019ff083-0588-7209-8403-3467f046b405"
            root = Path(home) / "profile" / ".pi" / "agent" / "sessions" / "project"
            root.mkdir(parents=True)
            path = root / f"timestamp_{session_id}.jsonl"
            path.write_text(json.dumps({
                "type": "session", "id": session_id, "cwd": "/project",
            }) + "\n", encoding="utf-8")
            with patch("herdr_web.app.Path.home", return_value=Path(home)):
                self.assertEqual(find_pi_session_by_id(session_id), path)
                self.assertIsNone(find_pi_session_by_id("../../private"))
                self.assertIsNone(find_pi_session_by_id("different-id"))

    def test_finds_most_recent_session_with_exact_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / ".pi" / "agent" / "sessions" / "project"
            root.mkdir(parents=True)
            older = root / "older.jsonl"
            newer = root / "newer.jsonl"
            other = root / "other.jsonl"
            for path, cwd in ((older, "/project"), (newer, "/project"), (other, "/other")):
                path.write_text(json.dumps({"type": "session", "id": path.stem, "cwd": cwd}) + "\n", encoding="utf-8")
            os.utime(older, (1, 1))
            os.utime(newer, (3, 3))
            os.utime(other, (4, 4))
            with patch("herdr_web.app.Path.home", return_value=Path(home)):
                self.assertEqual(find_recent_pi_session("/project"), newer)
                self.assertIsNone(find_recent_pi_session("/missing"))

    def test_reads_profile_independent_pane_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            backend = type("Backend", (), {
                "socket_path": Path(runtime) / "herdr-client.sock",
            })()
            api_socket = backend.socket_path.with_name("herdr.sock")
            session_file = Path(runtime) / "profile" / "sessions" / "session.jsonl"
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                path = pi_pane_mapping_path(api_socket, "w1:p2")
                path.parent.mkdir(mode=0o700)
                path.write_text(json.dumps({
                    "api_socket": str(api_socket),
                    "pane_id": "w1:p2",
                    "session_file": str(session_file),
                }), encoding="utf-8")
                path.chmod(0o600)

                self.assertEqual(read_pi_pane_mapping(backend, "w1:p2"), session_file)
                self.assertIsNone(read_pi_pane_mapping(backend, "other"))

    def test_reads_fresh_private_stream_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as runtime:
            session_file = Path(runtime) / "session.jsonl"
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}):
                path = pi_stream_path(session_file)
                path.parent.mkdir(mode=0o700)
                path.write_text(json.dumps({
                    "session_file": str(session_file),
                    "role": "assistant",
                    "markdown": "partial",
                    "streaming": True,
                    "updated_at": int(time.time() * 1000),
                }), encoding="utf-8")
                path.chmod(0o600)

                stream = read_pi_stream(session_file)

                self.assertIsNotNone(stream)
                self.assertEqual(stream["markdown"], "partial")
                self.assertTrue(stream["streaming"])
                path.write_text("{}", encoding="utf-8")
                self.assertIsNone(read_pi_stream(session_file))


if __name__ == "__main__":
    unittest.main()
