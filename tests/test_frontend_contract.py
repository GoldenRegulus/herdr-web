from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIRECTORY = PROJECT_ROOT / "herdr_web" / "static"


class FrontendContractTests(unittest.TestCase):
    def test_terminal_output_uses_supported_xterm_write_queue(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("_core.writeSync", application)
        self.assertIn("activeTerminal.write(bytes, () =>", application)
        self.assertIn("noteParsedOutput(flow, bytes.length)", application)

    def test_mouse_motion_is_coalesced_without_delaying_keys(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")

        self.assertIn("isDisposableMouseMotion(data)", application)
        self.assertIn("pendingMouseMotion = data", application)
        self.assertIn("queuePendingMouseMotion();\n      inputBuffer.append(data)", application)
        self.assertIn("MOUSE_MOTION_INTERVAL_MS = 16", application)

    def test_webgl_renderer_has_dom_fallback(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        vendor = STATIC_DIRECTORY / "vendor"

        self.assertIn("import './vendor/xterm-addon-webgl.js'", application)
        self.assertIn("activeTerminal.loadAddon(addon)", application)
        self.assertIn("addon.onContextLoss", application)
        self.assertIn("addon?.dispose()", application)
        self.assertTrue((vendor / "xterm-addon-webgl.js").is_file())
        self.assertTrue((vendor / "xterm-addon-webgl.LICENSE").is_file())

    def test_xterm_contains_synchronized_output_render_fix(self) -> None:
        xterm = (STATIC_DIRECTORY / "vendor" / "xterm.js").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "s?this._renderRows(e,t):this._renderDebouncer.refresh(e,t,this._rowCount)",
            xterm,
        )
        self.assertTrue(
            (STATIC_DIRECTORY / "vendor" / "xterm.SYNC-OUTPUT-PATCH.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
