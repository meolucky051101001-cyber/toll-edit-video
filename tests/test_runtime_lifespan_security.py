"""Exercise lifespan and middleware without starting or stopping any service."""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Request, Response
import main


CONTROL_PATHS = (
    "/api/stop-batch", "/api/pause-video", "/api/resume-video",
    "/api/audio-settings", "/api/worker-settings",
)


class RuntimeLifespanSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_invokes_existing_startup_helper_once(self):
        with patch.object(main, "_startup_background_tasks", new_callable=AsyncMock) as startup:
            async with main.app.router.lifespan_context(main.app):
                startup.assert_awaited_once_with()
            startup.assert_awaited_once_with()
        self.assertEqual(main.app.router.on_startup, [])
        self.assertIs(main.on_startup, main._startup_background_tasks)

    def request(self, path, token=None, origin=None):
        headers = [(b"host", b"testserver"), (b"x-dashboard-input", b"1")]
        if token is not None:
            headers.append((b"x-local-control-token", token.encode()))
        if origin:
            headers.append((b"origin", origin.encode()))
        return Request({"type": "http", "method": "POST", "path": path,
                        "headers": headers, "client": ("testclient", 123),
                        "server": ("testserver", 80), "scheme": "http", "query_string": b""})

    async def test_dashboard_header_cannot_bypass_token_for_any_control(self):
        for path in CONTROL_PATHS:
            for origin in (None, "http://127.0.0.1:8088", "http://localhost:5173"):
                for token in (None, "wrong-token"):
                    with self.subTest(path=path, origin=origin, token=token):
                        downstream = AsyncMock()
                        response = await main.security_middleware(self.request(path, token, origin), downstream)
                        self.assertEqual(response.status_code, 403)
                        downstream.assert_not_awaited()

    async def test_valid_token_reaches_each_control_without_running_endpoint(self):
        for path in CONTROL_PATHS:
            with self.subTest(path=path):
                downstream = AsyncMock(return_value=Response(status_code=204))
                response = await main.security_middleware(self.request(path, main.LOCAL_TOKEN), downstream)
                self.assertEqual(response.status_code, 204)
                downstream.assert_awaited_once()
