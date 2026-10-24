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
    session_preview,
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

    def test_session_preview_uses_versioned_assets(self) -> None:
        response = asyncio.run(session_preview())
        document = response.body.decode("utf-8")

        self.assertNotIn(STATIC_ASSET_PLACEHOLDER, document)
        self.assertIn("session-preview.css", document)
        self.assertIn("session-preview.js", document)
        self.assertIn("vendor/katex/katex.min.css", document)
        self.assertIn("vendor/katex/katex.min.js", document)
        self.assertIn("herdrWebKatexReady", document)
        self.assertIn("herdrWebMermaidReady", document)
        self.assertIn("vendor/mermaid/mermaid.min.js", document)
        self.assertIn("securityLevel: 'strict'", document)
        self.assertIn('id="message-navigation"', document)
        self.assertIn('id="message-toc"', document)
        self.assertIn('id="toc-toggle"', document)
        self.assertIn('id="message-toc-items"', document)
        self.assertIn('id="scratchpad-open"', document)
        self.assertIn('id="scratchpad-dialog"', document)
        self.assertIn('id="scratchpad-editor"', document)
        self.assertIn('id="scratchpad-preview"', document)
        self.assertIn('id="files-open"', document)
        self.assertIn('id="files-dialog"', document)
        self.assertIn('id="files-search"', document)
        self.assertIn('id="files-list"', document)
        self.assertIn('id="session-size"', document)
        self.assertIn('id="previous-message"', document)
        self.assertIn('id="next-message"', document)
        self.assertIn('id="toggle-all-messages"', document)
        self.assertIn('id="theme-toggle"', document)
        self.assertIn('class="message-toggle"', document)
        self.assertIn('class="message-peek"', document)
        self.assertIn('class="message-view-toggle"', document)
        self.assertIn('class="message-raw"', document)
        preview_script = (STATIC_DIR / "session-preview.js").read_text(encoding="utf-8")
        preview_style = (STATIC_DIR / "session-preview.css").read_text(encoding="utf-8")
        self.assertIn("function navigateMessage(offset)", preview_script)
        self.assertIn("scrollIntoView", preview_script)
        self.assertIn("event.altKey", preview_script)
        self.assertIn("data.live ? 1_000 : 3_000", preview_script)
        self.assertIn("document.hidden", preview_script)
        self.assertIn("visibilitychange", preview_script)
        self.assertIn("api/pi-sessions/", preview_script)
        self.assertIn("history.replaceState", preview_script)
        self.assertIn("function captureScrollAnchor", preview_script)
        self.assertIn("function restoreScrollAnchor", preview_script)
        self.assertIn("function renderMessageToc", preview_script)
        self.assertIn("new URLSearchParams({ limit: '20' })", preview_script)
        self.assertNotIn("new URLSearchParams({ limit: '10' })", preview_script)
        self.assertIn("function loadScratchpad", preview_script)
        self.assertIn("function loadMarkdownFiles", preview_script)
        self.assertIn("function markdownFileTree", preview_script)
        self.assertIn("function fileTreeDirectory", preview_script)
        self.assertIn("function fileTreeList", preview_script)
        self.assertIn("expandedFileDirectories", preview_script)
        self.assertIn("herdr-web-file-tree:${piSessionId}", preview_script)
        self.assertIn("filesOpen.hidden = false", preview_script)
        self.assertIn("function openMarkdownFile", preview_script)
        self.assertIn("documentData.document_mode", preview_script)
        self.assertIn("streamingWasExpanded", preview_script)
        self.assertIn("expandedMessageIds.add(finalized.id)", preview_script)
        self.assertIn("function saveScratchpad", preview_script)
        self.assertIn("function formatBytes", preview_script)
        self.assertIn("documentData.session_bytes", preview_script)
        self.assertIn("function applyScratchpadFormatting", preview_script)
        self.assertIn("setTimeout(() => void saveScratchpad(revision), 500)", preview_script)
        self.assertIn("function splitTableRow", preview_script)
        self.assertIn("function tableAlignments", preview_script)
        self.assertIn("function renderMarkdownTable", preview_script)
        self.assertIn("document.createElement('table')", preview_script)
        self.assertIn("function syncActiveTocHeading", preview_script)
        self.assertIn("activeButton.offsetTop", preview_script)
        self.assertIn("messageTocItems.scrollTop", preview_script)
        self.assertIn("rect.top <= readingLine && rect.bottom > readingLine", preview_script)
        self.assertIn("dataset.tocLevel", preview_script)
        self.assertIn("heading[1].length", preview_script)
        self.assertIn("distanceFromBottom < 80", preview_script)
        self.assertIn("function setMessageCollapsed", preview_script)
        self.assertIn("function setMessageRaw", preview_script)
        self.assertIn("rawMessageIds", preview_script)
        self.assertIn("message-raw code').textContent", preview_script)
        self.assertIn("function messageExcerpt", preview_script)
        self.assertIn("collapsedMessageIds", preview_script)
        self.assertIn("expandedMessageIds", preview_script)
        self.assertIn("const collapsed = documentData.document_mode ? false : !expandedMessageIds.has(message.id)", preview_script)
        self.assertIn("function toggleAll()", preview_script)
        self.assertIn("function appendMath", preview_script)
        self.assertIn("function appendExternalLink", preview_script)
        self.assertIn("(?:`[^`\\n]+`", preview_script)
        self.assertIn("appendExternalLink(parent, match[1]", preview_script)
        self.assertIn("token.startsWith('https://')", preview_script)
        self.assertIn("noopener noreferrer", preview_script)
        self.assertIn("globalThis.katex.render", preview_script)
        self.assertIn("trust: false", preview_script)
        self.assertIn("throwOnError: true", preview_script)
        self.assertIn("const displayDelimiter", preview_script)
        self.assertIn("function applyTheme", preview_script)
        self.assertIn("herdr-web-preview-theme", preview_script)
        self.assertIn("function renderMermaid", preview_script)
        self.assertIn("codeLanguage === 'mermaid'", preview_script)
        self.assertIn("securityLevel: 'strict'", preview_script)
        self.assertIn("data:image/svg+xml", preview_script)
        self.assertIn("Mermaid diagram", preview_script)
        self.assertNotIn("diagram.innerHTML", preview_script)
        self.assertIn(".math-display", preview_style)
        self.assertIn(".math-error", preview_style)
        self.assertIn(':root[data-preview-theme="light"]', preview_style)
        katex = STATIC_DIR / "vendor" / "katex"
        self.assertTrue((katex / "katex.min.js").is_file())
        self.assertTrue((katex / "katex.min.css").is_file())
        self.assertTrue((katex / "LICENSE").is_file())
        self.assertTrue((katex / "fonts" / "KaTeX_Main-Regular.woff2").is_file())
        mermaid = STATIC_DIR / "vendor" / "mermaid"
        self.assertTrue((mermaid / "mermaid.min.js").is_file())
        self.assertTrue((mermaid / "LICENSE").is_file())
        self.assertIn(".mermaid-diagram", preview_style)
        self.assertIn(".message-view-toggle", preview_style)
        self.assertIn(".message-raw", preview_style)
        self.assertIn(".markdown-table-wrap", preview_style)
        self.assertIn("#scratchpad-dialog", preview_style)
        self.assertIn("#scratchpad-body", preview_style)
        self.assertIn("#scratchpad-preview", preview_style)
        self.assertIn("#files-dialog", preview_style)
        self.assertIn(".files-tree", preview_style)
        self.assertIn(".files-directory-toggle", preview_style)
        self.assertIn(".files-tree-chevron", preview_style)
        self.assertIn(".files-item", preview_style)
        self.assertIn("#session-size", preview_style)
        self.assertIn("overflow-x: auto", preview_style)
        self.assertIn("#message-toc {", preview_style)
        self.assertIn('#message-toc-items button[data-level="2"]', preview_style)
        self.assertIn('#message-toc-items button[data-level="3"]', preview_style)
        self.assertIn("#toc-toggle", preview_style)
        self.assertNotIn("setInterval(refresh, 500)", preview_script)
        self.assertIn("#message-navigation {", preview_style)
        self.assertIn('.message[data-collapsed="true"] .message-peek', preview_style)
        self.assertIn("text-overflow: ellipsis", preview_style)
        self.assertIn("env(safe-area-inset-bottom)", preview_style)
        self.assertEqual(response.headers["cache-control"], NO_STORE_HEADERS["Cache-Control"])

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
