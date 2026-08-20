"""Resolve Herdr theme settings for the browser client."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Final, Literal

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ThemeAppearance = Literal["dark", "light"]

PALETTE_KEYS: Final = (
    "accent",
    "panel_bg",
    "surface0",
    "surface1",
    "surface_dim",
    "overlay0",
    "overlay1",
    "text",
    "subtext0",
    "mauve",
    "green",
    "yellow",
    "red",
    "blue",
    "teal",
    "peach",
)

# These values match the built-in palettes in Herdr 0.7.5.
THEME_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "catppuccin": ("#89b4fa", "#181825", "#313244", "#45475a", "#1e1e2e", "#6c7086", "#7f849c", "#cdd6f4", "#a6adc8", "#cba6f7", "#a6e3a1", "#f9e2af", "#f38ba8", "#89b4fa", "#94e2d5", "#fab387"),
    "catppuccin-latte": ("#1e66f5", "#eff1f5", "#ccd0da", "#bcc0cc", "#e6e9ef", "#9ca0b0", "#8c8fa1", "#4c4f69", "#6c6f85", "#8839ef", "#40a02b", "#df8e1d", "#d20f39", "#1e66f5", "#179299", "#fe640b"),
    "terminal": ("#3465a4", "#101216", "#101216", "#555753", "#555753", "#d3d7cf", "#eeeeec", "#e5e9f0", "#d3d7cf", "#d3d7cf", "#4e9a06", "#c4a000", "#ef2929", "#3465a4", "#06989a", "#c4a000"),
    "tokyo-night": ("#7aa2f7", "#1a1b26", "#24283b", "#414868", "#1a1b26", "#565f89", "#697196", "#c0caf5", "#a9b1d6", "#bb9af7", "#9ece6a", "#e0af68", "#f7768e", "#7aa2f7", "#7dcfff", "#ff9e64"),
    "tokyo-night-day": ("#2e7de9", "#e1e2e7", "#c4c8da", "#a8aecb", "#d2d3da", "#8990b3", "#68709a", "#3760bf", "#6172b0", "#7847bd", "#587539", "#8c6c3e", "#f52a65", "#2e7de9", "#118c74", "#b15c00"),
    "dracula": ("#bd93f9", "#282a36", "#44475a", "#6272a4", "#282a36", "#6272a4", "#828cb4", "#f8f8f2", "#d2d2dc", "#ff79c6", "#50fa7b", "#f1fa8c", "#ff5555", "#8be9fd", "#8be9fd", "#ffb86c"),
    "nord": ("#88c0d0", "#2e3440", "#3b4252", "#434c5e", "#2e3440", "#4c566a", "#646e82", "#eceff4", "#d8dee9", "#b48ead", "#a3be8c", "#ebcb8b", "#bf616a", "#81a1c1", "#8fbcbb", "#d08770"),
    "gruvbox": ("#d79921", "#282828", "#3c3836", "#504945", "#282828", "#928374", "#a89984", "#ebdbb2", "#d5c4a1", "#d3869b", "#b8bb26", "#fabd2f", "#fb4934", "#83a598", "#8ec07c", "#fe8019"),
    "gruvbox-light": ("#076678", "#fbf1c7", "#ebdbb2", "#d5c4a1", "#f2e5bc", "#928374", "#7c6f64", "#3c3836", "#504945", "#8f3f71", "#79740e", "#b57614", "#9d0006", "#076678", "#427b58", "#af3a03"),
    "one-dark": ("#61afef", "#282c34", "#2c313a", "#3e4451", "#282c34", "#5c6370", "#737a87", "#abb2bf", "#969ca8", "#c678dd", "#98c379", "#e5c07b", "#e06c75", "#61afef", "#56b6c2", "#d19a66"),
    "one-light": ("#4078f2", "#fafafa", "#f0f0f1", "#e5e5e6", "#f5f5f6", "#a0a1a7", "#686b77", "#383a42", "#686b77", "#a626a4", "#50a14f", "#c18401", "#e45649", "#4078f2", "#0184bc", "#986801"),
    "solarized": ("#268bd2", "#002b36", "#073642", "#586e75", "#002b36", "#586e75", "#657b83", "#93a1a1", "#839496", "#d33682", "#859900", "#b58900", "#dc322f", "#268bd2", "#2aa198", "#cb4b16"),
    "solarized-light": ("#268bd2", "#fdf6e3", "#eee8d5", "#93a1a1", "#eee8d5", "#93a1a1", "#586e75", "#657b83", "#839496", "#d33682", "#859900", "#b58900", "#dc322f", "#268bd2", "#2aa198", "#cb4b16"),
    "kanagawa": ("#7e9cd8", "#1f1f28", "#2a2a37", "#363646", "#1f1f28", "#727169", "#87867d", "#dcd7ba", "#c8c3aa", "#957fb8", "#76946a", "#c0a36e", "#c34043", "#7e9cd8", "#7fb4ca", "#ffa066"),
    "kanagawa-lotus": ("#4d699b", "#f2ecbc", "#dcd5ac", "#c9cbd1", "#d5cea3", "#a09cac", "#8a8980", "#545464", "#43436c", "#624c83", "#6f894e", "#77713f", "#c84053", "#4d699b", "#4e8ca2", "#cc6d00"),
    "rose-pine": ("#c4a7e7", "#191724", "#1f1d2e", "#26233a", "#191724", "#6e6a86", "#908caa", "#e0def4", "#c8c5dc", "#c4a7e7", "#31748f", "#f6c177", "#eb6f92", "#31748f", "#9ccfd8", "#ea9a97"),
    "rose-pine-dawn": ("#907aa9", "#faf4ed", "#f2e9e1", "#fffaf3", "#f2e9e1", "#9893a5", "#797593", "#464261", "#797593", "#907aa9", "#286983", "#ea9d34", "#b4637a", "#286983", "#56949f", "#d7827e"),
    "vesper": ("#ffc799", "#1a1a1a", "#232323", "#282828", "#101010", "#5c5c5c", "#7e7e7e", "#ffffff", "#a0a0a0", "#ffd1a8", "#99ffe4", "#ffc799", "#ff8080", "#b0b0b0", "#66ddcc", "#ffc799"),
}

ALIASES: Final = {
    "catppuccin-mocha": "catppuccin",
    "latte": "catppuccin-latte",
    "light": "catppuccin-latte",
    "tokyonight": "tokyo-night",
    "tokyo-day": "tokyo-night-day",
    "tokyonight-day": "tokyo-night-day",
    "gruvbox-dark": "gruvbox",
    "onedark": "one-dark",
    "onelight": "one-light",
    "solarized-dark": "solarized",
    "lotus": "kanagawa-lotus",
    "rosepine": "rose-pine",
    "rosepine-dawn": "rose-pine-dawn",
    "dawn": "rose-pine-dawn",
}

SIBLINGS: Final = {
    "catppuccin": ("catppuccin", "catppuccin-latte"),
    "catppuccin-latte": ("catppuccin", "catppuccin-latte"),
    "tokyo-night": ("tokyo-night", "tokyo-night-day"),
    "tokyo-night-day": ("tokyo-night", "tokyo-night-day"),
    "gruvbox": ("gruvbox", "gruvbox-light"),
    "gruvbox-light": ("gruvbox", "gruvbox-light"),
    "one-dark": ("one-dark", "one-light"),
    "one-light": ("one-dark", "one-light"),
    "solarized": ("solarized", "solarized-light"),
    "solarized-light": ("solarized", "solarized-light"),
    "kanagawa": ("kanagawa", "kanagawa-lotus"),
    "kanagawa-lotus": ("kanagawa", "kanagawa-lotus"),
    "rose-pine": ("rose-pine", "rose-pine-dawn"),
    "rose-pine-dawn": ("rose-pine", "rose-pine-dawn"),
}

ANSI_COLORS: Final = {
    "black": "#2e3436",
    "red": "#cc0000",
    "green": "#4e9a06",
    "yellow": "#c4a000",
    "blue": "#3465a4",
    "magenta": "#75507b",
    "purple": "#75507b",
    "cyan": "#06989a",
    "white": "#eeeeec",
    "gray": "#d3d7cf",
    "grey": "#d3d7cf",
    "darkgray": "#555753",
    "darkgrey": "#555753",
    "lightred": "#ef2929",
    "lightgreen": "#8ae234",
    "lightyellow": "#fce94f",
    "lightblue": "#729fcf",
    "lightmagenta": "#ad7fa8",
    "lightcyan": "#34e2e2",
}

HEX_COLOR = re.compile(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", re.IGNORECASE)
RGB_COLOR = re.compile(r"^rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$", re.IGNORECASE)
BACKGROUND_KEYS: Final = frozenset({"panel_bg", "surface0", "surface1", "surface_dim"})


def normalize_theme_name(name: str) -> str:
    normalized = name.lower().replace(" ", "-").replace("_", "-")
    return ALIASES.get(normalized, normalized)


def config_path() -> Path:
    configured = os.environ.get("HERDR_CONFIG_PATH")
    if configured:
        return Path(configured)
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "herdr" / "config.toml"
    return Path.home() / ".config" / "herdr" / "config.toml"


def _palette(name: str, fallback: str) -> tuple[str, dict[str, str]]:
    normalized = normalize_theme_name(name)
    values = THEME_VALUES.get(normalized)
    if values is None:
        normalized = fallback
        values = THEME_VALUES[fallback]
    return normalized, dict(zip(PALETTE_KEYS, values, strict=True))


def _parse_color(value: object, key: str, palette: dict[str, str]) -> str | None:
    if not isinstance(value, str):
        return None
    color = value.strip().lower()
    if color in {"reset", "default", "none", "transparent"}:
        terminal = dict(zip(PALETTE_KEYS, THEME_VALUES["terminal"], strict=True))
        if key == "panel_bg":
            return terminal["panel_bg"]
        if key in BACKGROUND_KEYS:
            return palette["panel_bg"]
        if key == "text":
            return terminal["text"]
        return palette["text"]
    match = HEX_COLOR.fullmatch(color)
    if match:
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(character * 2 for character in digits)
        return f"#{digits.lower()}"
    match = RGB_COLOR.fullmatch(color)
    if match:
        components = tuple(int(component) for component in match.groups())
        if all(component <= 255 for component in components):
            return "#" + "".join(f"{component:02x}" for component in components)
        return None
    return ANSI_COLORS.get(color, ANSI_COLORS["cyan"])


def resolve_theme(
    appearance: ThemeAppearance = "dark", path: Path | None = None
) -> dict[str, object]:
    """Read the Herdr config and return its effective browser palette."""
    configuration: dict[str, object] = {}
    try:
        configuration = tomllib.loads((path or config_path()).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        pass

    theme = configuration.get("theme")
    theme = theme if isinstance(theme, dict) else {}
    manual_name = theme.get("name") if isinstance(theme.get("name"), str) else "catppuccin"
    normalized_manual = normalize_theme_name(manual_name)
    dark_default, light_default = SIBLINGS.get(
        normalized_manual, (normalized_manual, normalized_manual)
    )
    auto_switch = theme.get("auto_switch") is True
    if auto_switch:
        configured_name = theme.get(f"{appearance}_name")
        requested_name = configured_name if isinstance(configured_name, str) else (
            light_default if appearance == "light" else dark_default
        )
        fallback = "catppuccin-latte" if appearance == "light" else "catppuccin"
    else:
        requested_name = manual_name
        fallback = "catppuccin"

    name, palette = _palette(requested_name, fallback)
    custom = theme.get("custom")
    if isinstance(custom, dict):
        for key in PALETTE_KEYS:
            if key not in custom:
                continue
            color = _parse_color(custom[key], key, palette)
            if color is not None:
                palette[key] = color

    ui = configuration.get("ui")
    custom_accent = custom.get("accent") if isinstance(custom, dict) else None
    if isinstance(ui, dict) and custom_accent is None:
        accent = ui.get("accent")
        if isinstance(accent, str) and accent != "cyan":
            color = _parse_color(accent, "accent", palette)
            if color is not None:
                palette["accent"] = color

    background = palette["panel_bg"]
    red, green, blue = (int(background[index : index + 2], 16) for index in (1, 3, 5))
    color_scheme = "light" if red * 299 + green * 587 + blue * 114 >= 128_000 else "dark"
    return {
        "name": name,
        "appearance": appearance,
        "color_scheme": color_scheme,
        "auto_switch": auto_switch,
        "palette": palette,
    }
