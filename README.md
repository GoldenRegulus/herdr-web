# herdr-web

`herdr-web` is a local browser terminal for existing Herdr sessions. It uses
xterm.js in the browser. It is a companion service, not a fork of Herdr, and it
does not expose the private Herdr socket to JavaScript.

It discovers the local user's conventional client sockets:

- `~/.config/herdr/herdr-client.sock` (the default session)
- `~/.config/herdr/sessions/*/herdr-client.sock` (named sessions)

The session picker can also start a named persistent session. The service runs
`herdr --session NAME`, waits for its client socket, and then stops only that
launcher client. The named Herdr server remains available.

When a user selects a session, the service starts the installed `herdr client`
in a private pseudo-terminal attached to that exact socket. The existing Herdr
client negotiates `TerminalAnsi` rendering, so the browser receives the same
server-owned TUI layout, colors, keyboard behavior, mouse protocol, panes, and
session capabilities as a terminal client. The browser is only a terminal
emulator.

## Install with uv

Requires Python 3.10 or later, `uv`, and an installed `herdr` client.

From a checkout:

```bash
uv sync
```

Run the checkout with `uv`:

```bash
uv run herdr-web --help
```

To install the command as a `uv` tool from GitHub:

```bash
uv tool install git+https://github.com/GoldenRegulus/herdr-web.git
herdr-web --help
```

## Run directly

Run this as the same Unix user that owns the Herdr server and sockets. You can
start it from inside a Herdr terminal; the web client connects as a separate
client:

```bash
uv run herdr-web --host 127.0.0.1 --port 8765
```

Open the service at:

```text
http://127.0.0.1:8765/
```

To allow direct access from the local network, bind all interfaces:

```bash
uv run herdr-web --host 0.0.0.0 --port 8765
```

Then open `http://192.168.1.1:8765/` from a client that can reach that host.
Replace `192.168.1.1` with the host's real LAN address.

To bind only one interface, use that address instead:

```bash
uv run herdr-web --host 192.168.1.1 --port 8765
```

The default bind address is `127.0.0.1` for safety. Herdr Web does not include
a login system. Anyone who can reach the service can control a selected Herdr
session and start named sessions. Use an unrestricted LAN bind only on a
trusted network. For authenticated remote access, keep Herdr Web on loopback
and put an OIDC-capable reverse proxy in front of it. The proxy must support
WebSocket upgrades and long-lived streaming responses.

A reverse proxy can use `GET /healthz` for a process health check. Herdr Web
does not consume identity headers from the proxy.

## Run behind Jupyter or SageMaker

The same process supports direct access, Jupyter Server Proxy, and SageMaker
Code Editor proxy prefixes. If the Jupyter server runs on the same host, bind
to `0.0.0.0` when you need both LAN access and the Jupyter proxy:

```bash
uv run herdr-web --host 0.0.0.0 --port 8765
```

Open it through Jupyter Server Proxy:

```text
https://JUPYTER_HOST/USER_BASE/proxy/8765/
```

For example, a JupyterHub user server commonly uses:

```text
https://hub.example.org/user/alice/proxy/8765/
```

If only Jupyter access is needed, bind to `127.0.0.1` instead. A process bound
only to `192.168.1.1` accepts direct connections to that address, but a proxy
that connects to `127.0.0.1` cannot reach it.

The browser derives the complete proxy prefix from its visible URL. It applies
that prefix to local assets, HTTP requests, and the WebSocket URL. This also
works with prefixes such as `/codeeditor/default/proxy/8765/` when SageMaker
Code Editor provides that proxy route. The proxy must support WebSocket
upgrades. If it does not, the browser uses the HTTP fallback.

Each server process puts its complete static asset set in a new versioned
path. The index response is not cached, and all JS, CSS, and font references
use the same version. A server restart therefore cannot combine files from two
releases, even when a Jupyter proxy or browser retains older assets.

The package includes xterm.js 6.0.0, the fit addon 0.11.0, and the WebGL addon
0.19.0 under `herdr_web/static/vendor/`. The terminal does not require a CDN or
internet access. Their MIT licenses are in `xterm.LICENSE`,
`xterm-addon-fit.LICENSE`, and `xterm-addon-webgl.LICENSE` in that directory.
The vendored xterm.js contains a focused fix for continuous DEC 2026
synchronized-output rendering. See `xterm.SYNC-OUTPUT-PATCH.md` in the same
directory.

The package also includes the prebuilt MesloLGS NF Nerd Font web fonts under
`herdr_web/static/fonts/`. This Powerlevel10k-patched font includes terminal
symbols. xterm.js draws standard box characters with its connected custom
glyphs. The browser loads the regular, bold, italic, and bold-italic files
before it measures the terminal. The font license
and copyright notice are in `herdr_web/static/fonts/MesloLGSNF.LICENSE`.

## Theme synchronization

The browser reads Herdr's active `[theme]` configuration. It applies the same
built-in palette and `[theme.custom]` overrides to the complete web interface
and to xterm.js. The terminal background, foreground, cursor, selection, and
16 ANSI colors use Herdr's semantic color tokens.

The browser checks the theme every two seconds. A theme saved in Herdr Settings
therefore updates the web interface without a page reload. When
`theme.auto_switch` is enabled, the browser selects `dark_name` or `light_name`
from its system color preference. Theme synchronization only changes the local
browser UI and xterm instance. It does not write configuration or inject theme
changes into Herdr.

The synchronized built-ins are Catppuccin, Terminal, Tokyo Night, Dracula,
Nord, Gruvbox, One Dark, Solarized, Kanagawa, Rosé Pine, Vesper, and their
light variants. The service uses `HERDR_CONFIG_PATH` and `XDG_CONFIG_HOME` with
the same precedence as Herdr.

## Slow connections

The WebSocket bridge keeps one PTY output chunk in its application queue. It
coalesces each output burst for at most 2 ms and up to 256 KiB. This reduces
WebSocket and parser calls without dropping ANSI bytes. The browser always uses
xterm.js's supported scheduled write queue. It acknowledges each chunk only
after xterm.js parses it. This queue limits parser work per browser task and
lets the renderer paint between batches. Cumulative acknowledgements limit data
waiting in the WebSocket and xterm.js. This early backpressure lets Herdr apply
its native slow-client frame coalescing.

Herdr wraps rendered frames in DEC 2026 synchronized-output markers. The local
xterm.js patch paints a completed synchronized frame before the next frame can
suppress its pending render. It does not change normal unsynchronized output.
The WebGL2 renderer keeps high-rate output work out of xterm.js's slower DOM
renderer. This keeps browser input responsive during dense animation. The
terminal automatically uses the DOM renderer if WebGL2 is unavailable or if
the browser loses its WebGL context.

Browser input uses a reusable byte buffer and `TextEncoder.encodeInto()`. It
combines input into small binary WebSocket messages and limits the browser's
native WebSocket backlog. Interactive input drains in its browser event task,
so continuous output cannot starve it behind a timer. Applications that enable
continuous mouse tracking can produce pointer positions faster than they can
consume them. The browser keeps the latest adjacent motion at 16 ms intervals.
It flushes that position before each key, click, wheel event, paste, or image,
so non-disposable input stays ordered and lossless. Input stays queued while
the connection attaches. The HTTP fallback sends one input request at a time,
so requests cannot pass each other.

## Mobile navigation

On a narrow or coarse-pointer screen, the browser shows a native toolbar with
Spaces, Tabs, Agents, and More controls. The bottom sheets read structured
state from `herdr api snapshot`. Focus actions use the public Herdr workspace,
tab, and agent CLI commands. The browser does not parse terminal ANSI to find
navigation state.

Herdr 0.7.5 still renders its permanent terminal UI around the focused pane.
The native toolbar covers the terminal-drawn mobile switch area. Complete
pane-only rendering needs a future Herdr client presentation capability; Herdr
Web does not crop or discard the ordered terminal stream to simulate it.

## Clipboard images

When the browser has a WebSocket connection, paste PNG, JPEG, GIF, WebP, or
BMP images into the terminal. The bridge stages the image in a private
temporary directory and sends its path through Herdr's existing remote-client
paste path. The maximum image size is 16 MiB.

The HTTP fallback supports terminal text input but not binary image uploads.

## Configuration

| Option or variable | Purpose |
| --- | --- |
| `--host HOST` | Interface to bind. Defaults to `127.0.0.1`. |
| `--port PORT` | Port to bind. Defaults to `8765`. |
| `HERDR_BINARY` | Absolute path to the Herdr binary used for the local client. Defaults to `herdr` on `PATH`. |
| `HERDR_WEB_CONFIG_DIR` | Herdr configuration directory to scan. Defaults to `~/.config/herdr`, respecting `XDG_CONFIG_HOME`. |
| `HERDR_CONFIG_PATH` | Herdr config file used for synchronized theme settings. Uses Herdr's normal config path when unset. |

Only sockets found under the selected config directory are attachable. The web
client deliberately has no endpoint that accepts an arbitrary Unix-socket path.

## Current scope

The application provides backend selection and an interactive,
terminal-faithful Herdr view. It intentionally uses the shipped Herdr client
rather than duplicating its unstable bincode client protocol in JavaScript.

Not yet implemented: multiple browser clients coordinated for one backend, or
discovery of Herdr sessions outside the conventional config directory.
