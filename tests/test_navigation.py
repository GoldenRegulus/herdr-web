import inspect
import json
import unittest

from starlette.websockets import WebSocket

from herdr_web import app as application
from herdr_web.app import project_navigation_snapshot, websocket_origin_is_allowed


class NavigationTests(unittest.TestCase):
    def test_browser_snapshot_excludes_private_paths(self) -> None:
        document = {
            "result": {
                "snapshot": {
                    "version": 3,
                    "focused_workspace_id": "w1",
                    "focused_tab_id": "w1:t1",
                    "focused_pane_id": "w1:p1",
                    "workspaces": [
                        {
                            "workspace_id": "w1",
                            "label": "project",
                            "focused": True,
                            "cwd": "/private/project",
                        }
                    ],
                    "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1"}],
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "tab_id": "w1:t1",
                            "workspace_id": "w1",
                            "terminal_title_stripped": "agent",
                            "cwd": "/private/project",
                            "agent_session": {"value": "/private/session.jsonl"},
                        }
                    ],
                    "agents": [
                        {
                            "pane_id": "w1:p1",
                            "tab_id": "w1:t1",
                            "workspace_id": "w1",
                            "agent": "pi",
                            "agent_status": "working",
                            "agent_session": {"value": "/private/session.jsonl"},
                        }
                    ],
                }
            }
        }

        snapshot = project_navigation_snapshot(document)

        encoded = json.dumps(snapshot)
        self.assertNotIn("/private", encoded)
        self.assertNotIn("agent_session", encoded)
        self.assertEqual(snapshot["agents"][0]["agent_status"], "working")

    def test_snapshot_requires_the_public_api_shape(self) -> None:
        with self.assertRaises(RuntimeError):
            project_navigation_snapshot({"result": {}})


class TransportSecurityTests(unittest.TestCase):
    def test_server_does_not_trust_forwarded_source_addresses(self) -> None:
        self.assertIn("proxy_headers=False", inspect.getsource(application.main))

    def test_websocket_origin_must_match_host(self) -> None:
        def websocket(origin: str | None) -> WebSocket:
            async def receive():
                return {"type": "websocket.disconnect"}

            async def send(_message):
                return None

            headers = [(b"host", b"herdr.example:8765")]
            if origin is not None:
                headers.append((b"origin", origin.encode()))
            return WebSocket(
                {
                    "type": "websocket",
                    "path": "/ws/test",
                    "headers": headers,
                    "client": ("127.0.0.1", 12345),
                },
                receive,
                send,
            )

        self.assertTrue(
            websocket_origin_is_allowed(websocket("https://herdr.example:8765"))
        )
        self.assertFalse(
            websocket_origin_is_allowed(websocket("https://attacker.example"))
        )
        self.assertTrue(websocket_origin_is_allowed(websocket(None)))


if __name__ == "__main__":
    unittest.main()
