import asyncio
from pathlib import Path
import subprocess
import sys
import unittest

from fastapi import Request
from fastapi.responses import HTMLResponse

from herdr_web.app import (
    NO_STORE_HEADERS,
    STATIC_ASSET_PLACEHOLDER,
    STATIC_ASSET_VERSION,
    index,
    set_static_cache_headers,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StaticAssetTests(unittest.TestCase):
    def test_index_uses_one_versioned_asset_directory(self) -> None:
        response = asyncio.run(index())
        document = response.body.decode("utf-8")
        asset_directory = f"static/{STATIC_ASSET_VERSION}/"

        self.assertNotIn(STATIC_ASSET_PLACEHOLDER, document)
        self.assertIn(
            f"`${{window.herdrWebBasePath}}{asset_directory}`", document
        )
        self.assertIn("`${window.herdrWebAssetBasePath}style.css`", document)
        self.assertIn("`${window.herdrWebAssetBasePath}app.js`", document)
        self.assertEqual(response.headers["cache-control"], NO_STORE_HEADERS["Cache-Control"])

    def test_static_cache_headers_match_asset_path(self) -> None:
        async def response_for(path: str) -> HTMLResponse:
            scope = {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "server": ("test", 80),
                "client": ("test", 1),
                "root_path": "",
            }
            request = Request(scope)

            async def call_next(_request: Request) -> HTMLResponse:
                return HTMLResponse("")

            return await set_static_cache_headers(request, call_next)

        versioned = asyncio.run(
            response_for(f"/static/{STATIC_ASSET_VERSION}/app.js")
        )
        compatibility = asyncio.run(response_for("/static/app.js"))
        api = asyncio.run(response_for("/api/backends"))

        self.assertIn("immutable", versioned.headers["cache-control"])
        self.assertEqual(
            compatibility.headers["cache-control"], NO_STORE_HEADERS["Cache-Control"]
        )
        self.assertNotIn("cache-control", api.headers)

    def test_new_process_gets_new_asset_version(self) -> None:
        command = [
            sys.executable,
            "-c",
            "from herdr_web.app import STATIC_ASSET_VERSION; print(STATIC_ASSET_VERSION)",
        ]
        first = subprocess.check_output(command, cwd=PROJECT_ROOT, text=True).strip()
        second = subprocess.check_output(command, cwd=PROJECT_ROOT, text=True).strip()

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
