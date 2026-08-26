import asyncio
import inspect
import json
import unittest
from unittest.mock import AsyncMock, patch

from starlette.websockets import WebSocket

from herdr_web import app as application
from herdr_web.app import (
    Backend,
    project_navigation_snapshot,
    query_tailscale_login,
    tailscale_request_is_allowed,
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


class TailscaleAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        application.tailscale_identity_cache.clear()
        application.tailscale_identity_locks.clear()

    async def test_whois_returns_verified_login(self) -> None:
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (
            json.dumps(
                {"UserProfile": {"LoginName": "mradityadash@gmail.com"}}
            ).encode(),
            b"",
        )
        with patch(
            "herdr_web.app.asyncio.create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            login = await query_tailscale_login("100.119.149.107")

        self.assertEqual(login, "mradityadash@gmail.com")

    async def test_only_configured_login_is_allowed(self) -> None:
        with (
            patch("herdr_web.app.allowed_tailscale_user", "mradityadash@gmail.com"),
            patch(
                "herdr_web.app.query_tailscale_login",
                AsyncMock(side_effect=["mradityadash@gmail.com", "other@example.com"]),
            ),
        ):
            self.assertTrue(await tailscale_request_is_allowed("100.1.1.1"))
            self.assertFalse(await tailscale_request_is_allowed("100.1.1.2"))
            self.assertFalse(await tailscale_request_is_allowed(None))

    def test_server_does_not_trust_forwarded_source_addresses(self) -> None:
        self.assertIn("proxy_headers=False", inspect.getsource(application.main))

    def test_websocket_origin_must_match_host(self) -> None:
        def websocket(origin: str) -> WebSocket:
            async def receive():
                return {"type": "websocket.disconnect"}

            async def send(_message):
                return None

            return WebSocket(
                {
                    "type": "websocket",
                    "path": "/ws/test",
                    "headers": [
                        (b"host", b"100.70.11.77:8765"),
                        (b"origin", origin.encode()),
                    ],
                    "client": ("100.119.149.107", 12345),
                },
                receive,
                send,
            )

        with patch("herdr_web.app.allowed_tailscale_user", "mradityadash@gmail.com"):
            self.assertTrue(
                websocket_origin_is_allowed(websocket("http://100.70.11.77:8765"))
            )
            self.assertFalse(
                websocket_origin_is_allowed(websocket("https://attacker.example"))
            )


if __name__ == "__main__":
    unittest.main()
