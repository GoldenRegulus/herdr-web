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

The default **Full** mode starts the installed `herdr client` in a private
pseudo-terminal attached to that exact socket. The existing Herdr client
negotiates `TerminalAnsi` rendering, so the browser receives the same
server-owned TUI layout, colors, keyboard behavior, mouse protocol, panes, and
session capabilities as a terminal client.

The optional **Panes** mode uses `herdr api snapshot` for workspace, tab, pane,
and split geometry. It uses Herdr's public `terminal session control` stream for
each visible pane. Wide screens show the active tab's split layout. Narrow
screens show one selected pane. Inactive tabs and hidden panes continue to run
inside Herdr.

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
The vendored xterm.js contains focused fixes for continuous DEC 2026
synchronized-output rendering and native WebKit text Paste delivery. See
`xterm.SYNC-OUTPUT-PATCH.md` and `xterm.MOBILE-PASTE-PATCH.md` in the same
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
its native slow-client frame coalescing. Panes mode acknowledges each pane
frame after its target xterm parser completes. Each pane stream waits for that
acknowledgement before it reads another frame.

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

## Panes mode

Select **Panes** in the session header. The browser reads structured state from
`herdr api snapshot`; it does not parse terminal ANSI to find navigation or
pane boundaries.

On a wide screen, the active tab displays all its panes with Herdr's split
geometry. Each pane has an independent xterm surface and terminal-session
stream. Only the active tab is attached through the web client.

On a narrow or coarse-pointer screen, Panes is the only presentation mode and
one pane fills the available area. The top title shows
`Space · Tab · Pane`. The top-right transport state is passive while the
connection is healthy. It becomes a reconnect control only after a disconnect.
The hamburger button opens one bottom-sheet navigator. It shows a flat,
edge-to-edge tree of spaces and tabs, with pane children only when a tab has
more than one pane. Space and multi-pane tab branches expand or collapse in
the sheet. Plain gutters show each nesting level. Each tree row has a thin
accent edge, and the active row has a stronger edge. Tab and pane rows are more
compact than space rows. The sheet stays open until you select a tab or pane leaf. Terminal
size controls follow the tree, and **Sessions** is in the sheet header. The
sheet slides in and out unless reduced motion is active. Opening it does not
resize the terminal. Swipe down to
read older output and swipe up to move toward newer output. A release keeps the measured touch
velocity and then decelerates. Reduced-motion settings disable the inertial
continuation.

The compact two-row mobile control bar has persistent **Ctrl**, **Alt**, and
**Shift** toggles, **Esc**, **Tab**, a keyboard lock, and Left, Up, Down, and
Right buttons. Select **Nav** to replace the lower row with Terminal Snapshot,
Home, Page Up, Page Down, End, and forward Delete. **Nav** is a display-layer
control; Herdr Web does not send it to the terminal as a modifier. The
modifiers apply to arrow and navigation buttons and to a single character from
the software keyboard. Shift+Tab sends Backtab. A modified software-keyboard
Enter sends CSI-u with the active Ctrl, Alt, and Shift combination. Thus,
terminal applications can distinguish each combination from plain Enter. Hold
an arrow or navigation key to repeat it. Selecting another pane clears the
modifiers. The keyboard lock prevents a
terminal tap from opening the software keyboard. Use **Browse → Terminal
size** to set terminal text from 10 px to 24 px. The browser saves this setting
on the device.

Xterm uses complete terminal rows. Herdr Web moves the incomplete-row
remainder to the top title padding so the xterm screen ends at the mobile
control bar. Like the music app, Herdr Web leaves `viewport-fit` at its default
value. When a mobile software keyboard is closed, the control bar reserves at
least one terminal-cell row below its keys for system navigation controls. This
extra clearance is removed when the visual viewport shows that the keyboard is
open.

The mouse button has **off** and **on** states. Mouse input is off by
default. In the off state, Herdr Web sends no terminal mouse reports and the
live terminal does not permit text selection. A quick vertical swipe scrolls
Herdr. A tap opens the keyboard without sending a terminal mouse report. The
transparent helper on the cursor row remains the native text and image Paste
target. Use Terminal Snapshot when you must select or copy terminal text.

When mouse input is on, a tap sends a left-click at the touched cell and opens
the keyboard. A quick vertical swipe scrolls Herdr. A short pause followed by
movement starts a left-button drag. A 500 ms hold sends a right-click. Hardware
hover, buttons, drags, and wheel input send SGR mouse reports to the terminal.
Text selection is off. Panes queues the atomic tap and 500 ms hold clicks as
structured WebSocket operations and writes them with Herdr's public
`pane.send_text` API. These clicks do not apply the terminal-session
controller's keyboard follow-to-bottom policy.

The camera button in the mobile **Nav** row opens a terminal snapshot. Herdr Web
copies the visible cells, colors, rows, and columns into a disconnected,
read-only xterm instance. The live terminal continues to parse and acknowledge
output behind it. The snapshot does not send keyboard or mouse input, and its
text stays fixed for native selection and Copy. Close the snapshot to return
to the latest live view. Herdr Web does not create an image screenshot, text
area, or fake terminal text layer.

Text Paste in Panes uses Herdr's public `pane.send_input` API. This keeps LF
line breaks and lets Herdr apply its server-side bracketed-paste state instead
of converting each line break to an Enter key. Herdr Web uses only xterm's
internal keyboard helper. When mouse input is off, the transparent 16 px helper
covers the complete terminal row that contains the cursor. The keyboard lock
blocks terminal taps from focusing it. Bottom control buttons preserve an
already-open keyboard but do not open a closed keyboard. When iOS reports
`deleteWordBackward`, Herdr Web sends
Meta+Backspace as `ESC DEL`. Some iOS versions report only repeated Backspace
keys because xterm prevents native text edits. For these versions, Herdr Web
keeps the first 22 deletions as single-character Backspace and sends `ESC DEL`
from the 23rd continuous deletion. A 400 ms pause resets this count. The
terminal application applies its normal Option+Backspace or Alt+Backspace
binding.

Herdr Web follows the iOS visual viewport while the keyboard opens and closes.
The control bar stays above the keyboard, xterm fits the available area, and
Herdr receives the new row and column size. Herdr Web combines adjacent scroll
steps before it sends them to reduce scroll-command backlog.

iOS does not give a web page the raw movement distance from its spacebar
trackpad. It moves a caret inside an editable text model. Xterm's keyboard
helper has no matching terminal-line model, so Herdr Web cannot convert that
gesture to matching terminal cursor movement. Use the repeatable arrow
controls for terminal cursor movement.

Selecting a pane or tab replaces only the web streams. It does not stop the
terminal processes in Herdr. If another direct client controls a pane, Panes
mode shows that pane through Herdr's read-only observer instead of taking
control.

Panes mode requires a WebSocket. Full mode remains available on desktop and
keeps the HTTP fallback. Browser chrome uses row and column units measured
from xterm. Any sub-cell remainder from a browser viewport or split uses the
same Herdr terminal background instead of xterm's black fallback.

Herdr 0.7.5's terminal-session stream does not send all host-terminal mode
changes. Panes mouse input is therefore an explicit user setting. Use desktop
Full mode for automatic terminal mouse-mode handling or enhanced keyboard
modes.

## Installed web app

`/manifest.webmanifest` defines Herdr Web with ID `/`, start URL `/`, scope
`/`, and standalone display. Apple standalone metadata is in the root page.
The page uses the default contained iOS viewport, as the music app does. Add
Herdr Web to the home screen and launch it
from there to remove normal browser controls. A normal browser tab keeps its
browser controls.

## Clipboard images

In Full mode with a WebSocket connection, paste PNG, JPEG, GIF, WebP, or BMP
images into the terminal. The bridge stages the image in a private temporary
directory and sends its path through Herdr's existing remote-client paste path.
The maximum image size is 16 MiB.

Panes mode accepts the same image formats and size limit over its multiplexed
WebSocket. Because Herdr Web and each pane run on the same host, the bridge
stages the image in a private file and sends that absolute path through Herdr's
public `pane.send_input` API. Herdr applies the pane runtime's bracketed-paste
state. The staged file remains available for the Panes WebSocket lifetime and
is removed when that connection closes. The HTTP fallback does not support
binary image uploads. On mobile, tap the terminal to open the iOS keyboard for
typing or Paste. Herdr Web does not add a terminal selection overlay or
clipboard editor.

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

The application provides backend selection, the terminal-faithful Full view,
and a responsive browser-native Panes view. It uses the shipped Herdr client
and public terminal-session bridge. It does not duplicate Herdr's private
bincode protocol in JavaScript.

Not yet implemented: HTTP fallback for Panes mode, layout editing,
coordinated takeover between browser clients, or discovery of
Herdr sessions outside the conventional config directory.
