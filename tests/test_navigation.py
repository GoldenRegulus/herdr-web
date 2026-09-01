import inspect
import json
import unittest

from starlette.websockets import WebSocket

from herdr_web import app as application
from herdr_web.app import (
    parse_pane_stream_requests,
    project_navigation_snapshot,
    websocket_origin_is_allowed,
)


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
                            "terminal_id": "term_private",
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
                    "layouts": [
                        {
                            "workspace_id": "w1",
                            "tab_id": "w1:t1",
                            "zoomed": False,
                            "focused_pane_id": "w1:p1",
                            "area": {"x": 0, "y": 0, "width": 80, "height": 24},
                            "panes": [
                                {
                                    "pane_id": "w1:p1",
                                    "focused": True,
                                    "rect": {"x": 0, "y": 0, "width": 80, "height": 24},
                                    "cwd": "/private/project",
                                }
                            ],
                            "private": "/private/layout",
                        }
                    ],
                }
            }
        }

        snapshot = project_navigation_snapshot(document)

        encoded = json.dumps(snapshot)
        self.assertNotIn("/private", encoded)
        self.assertNotIn("agent_session", encoded)
        self.assertNotIn("term_private", encoded)
        self.assertEqual(snapshot["agents"][0]["agent_status"], "working")
        self.assertEqual(snapshot["layouts"][0]["panes"][0]["pane_id"], "w1:p1")
        self.assertNotIn("private", snapshot["layouts"][0])

    def test_snapshot_requires_the_public_api_shape(self) -> None:
        with self.assertRaises(RuntimeError):
            project_navigation_snapshot({"result": {}})

    def test_snapshot_rejects_invalid_layout_geometry(self) -> None:
        document = {
            "result": {
                "snapshot": {
                    "layouts": [
                        {
                            "workspace_id": "w1",
                            "tab_id": "t1",
                            "area": {"x": 0, "y": 0, "width": 80, "height": 24},
                            "panes": [
                                {
                                    "pane_id": "p1",
                                    "rect": {"x": 70, "y": 0, "width": 20, "height": 24},
                                }
                            ],
                        }
                    ]
                }
            }
        }

        with self.assertRaisesRegex(RuntimeError, "outside its layout"):
            project_navigation_snapshot(document)

    def test_pane_stream_requests_use_current_tab_panes(self) -> None:
        snapshot = {
            "tabs": [{"tab_id": "t1"}],
            "panes": [
                {"pane_id": "p1", "tab_id": "t1"},
                {"pane_id": "p2", "tab_id": "t2"},
            ],
        }
        tab_id, requests = parse_pane_stream_requests(
            {
                "tab_id": "t1",
                "panes": [{"stream_id": 7, "pane_id": "p1", "cols": 80, "rows": 24}],
            },
            snapshot,
        )

        self.assertEqual(tab_id, "t1")
        self.assertEqual(requests[0].pane_id, "p1")
        with self.assertRaisesRegex(ValueError, "unavailable pane"):
            parse_pane_stream_requests(
                {
                    "tab_id": "t1",
                    "panes": [
                        {"stream_id": 1, "pane_id": "p2", "cols": 80, "rows": 24}
                    ],
                },
                snapshot,
            )


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
