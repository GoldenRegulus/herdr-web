import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from herdr_web.theme import config_path, resolve_theme


class ThemeTests(unittest.TestCase):
    def write_config(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_default_theme_is_catppuccin(self) -> None:
        theme = resolve_theme(path=Path("/missing/herdr/config.toml"))

        self.assertEqual(theme["name"], "catppuccin")
        self.assertEqual(theme["color_scheme"], "dark")
        self.assertEqual(theme["palette"]["panel_bg"], "#181825")
        self.assertEqual(theme["palette"]["text"], "#cdd6f4")

    def test_auto_switch_uses_builtin_sibling(self) -> None:
        path = self.write_config(
            '[theme]\nname = "tokyo-night"\nauto_switch = true\n'
        )

        dark = resolve_theme("dark", path)
        light = resolve_theme("light", path)

        self.assertEqual(dark["name"], "tokyo-night")
        self.assertEqual(light["name"], "tokyo-night-day")
        self.assertEqual(light["color_scheme"], "light")
        self.assertEqual(light["palette"]["panel_bg"], "#e1e2e7")

    def test_auto_switch_accepts_explicit_names(self) -> None:
        path = self.write_config(
            """[theme]
name = "nord"
auto_switch = true
dark_name = "vesper"
light_name = "one-light"
"""
        )

        self.assertEqual(resolve_theme("dark", path)["name"], "vesper")
        self.assertEqual(resolve_theme("light", path)["name"], "one-light")

    def test_custom_colors_match_herdr_formats(self) -> None:
        path = self.write_config(
            """[theme]
name = "dracula"
[theme.custom]
accent = "#abc"
panel_bg = "reset"
red = "rgb(1, 2, 255)"
green = "lightgreen"
"""
        )

        palette = resolve_theme(path=path)["palette"]

        self.assertEqual(palette["accent"], "#aabbcc")
        self.assertEqual(palette["panel_bg"], "#101216")
        self.assertEqual(palette["red"], "#0102ff")
        self.assertEqual(palette["green"], "#8ae234")

    def test_custom_accent_wins_over_legacy_ui_accent(self) -> None:
        path = self.write_config(
            """[theme.custom]
accent = "#123456"
[ui]
accent = "red"
"""
        )

        self.assertEqual(resolve_theme(path=path)["palette"]["accent"], "#123456")

    def test_config_path_matches_herdr_environment_rules(self) -> None:
        with patch.dict(os.environ, {"HERDR_CONFIG_PATH": "/tmp/custom.toml"}):
            self.assertEqual(config_path(), Path("/tmp/custom.toml"))
        with patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": "/tmp/config-home"},
            clear=True,
        ):
            self.assertEqual(
                config_path(), Path("/tmp/config-home/herdr/config.toml")
            )

    def test_invalid_config_uses_default_theme(self) -> None:
        path = self.write_config("[theme\ninvalid")

        self.assertEqual(resolve_theme(path=path)["name"], "catppuccin")


if __name__ == "__main__":
    unittest.main()
