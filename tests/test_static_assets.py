import asyncio
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from fastapi import Request
from fastapi.responses import HTMLResponse

from herdr_web.app import (
    NO_STORE_HEADERS,
    PWA_MANIFEST,
    STATIC_ASSET_PLACEHOLDER,
    STATIC_ASSET_VERSION,
    STATIC_DIR,
    STATIC_SNAPSHOT_DIR,
    health,
    index,
    set_static_cache_headers,
    snapshot_static_directory,
    web_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StaticAssetTests(unittest.TestCase):
    def test_health_check_has_no_session_dependency(self) -> None:
        self.assertEqual(asyncio.run(health()), {"status": "ok"})

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
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', document)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', document)
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1"', document)
        self.assertNotIn("viewport-fit=cover", document)

    def test_manifest_uses_root_scope_and_standalone_display(self) -> None:
        response = asyncio.run(web_manifest())

        self.assertEqual(PWA_MANIFEST["id"], "/")
        self.assertEqual(PWA_MANIFEST["start_url"], "/")
        self.assertEqual(PWA_MANIFEST["scope"], "/")
        self.assertEqual(PWA_MANIFEST["display"], "standalone")
        self.assertEqual(response.media_type, "application/manifest+json")
        self.assertEqual(response.headers["cache-control"], NO_STORE_HEADERS["Cache-Control"])
        self.assertTrue((STATIC_DIR / "icons" / "herdr.svg").is_file())
        self.assertTrue((STATIC_DIR / "icons" / "herdr-180.png").is_file())
        self.assertTrue((STATIC_DIR / "icons" / "herdr-192.png").is_file())
        self.assertTrue((STATIC_DIR / "icons" / "herdr-512.png").is_file())

    def test_process_uses_an_immutable_static_snapshot(self) -> None:
        self.assertNotEqual(STATIC_SNAPSHOT_DIR, STATIC_DIR)
        self.assertEqual(
            (STATIC_SNAPSHOT_DIR / "app.js").read_bytes(),
            (STATIC_DIR / "app.js").read_bytes(),
        )

        with tempfile.TemporaryDirectory(dir="/tmp") as directory_name:
            source = Path(directory_name) / "source"
            source.mkdir()
            (source / "asset.js").write_text("first", encoding="utf-8")
            root, snapshot = snapshot_static_directory(source)
            try:
                (source / "asset.js").write_text("second", encoding="utf-8")
                self.assertEqual(
                    (snapshot / "asset.js").read_text(encoding="utf-8"), "first"
                )
            finally:
                shutil.rmtree(root, ignore_errors=True)

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
