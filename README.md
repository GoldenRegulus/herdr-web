# herdr-web

`herdr-web` is a local browser terminal for existing Herdr sessions. It is a
companion service, not a fork of Herdr, and it does not expose the private
Herdr socket to JavaScript.

It discovers the local user's conventional client sockets:

- `~/.config/herdr/herdr-client.sock` (the default session)
- `~/.config/herdr/sessions/*/herdr-client.sock` (named sessions)

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

Run this as the same Unix user that owns the Herdr server and sockets:

```bash
uv run herdr-web --host 127.0.0.1 --port 8766
```

Open the service at:

```text
http://127.0.0.1:8766/
```

To allow direct access from the local network, bind all interfaces:

```bash
uv run herdr-web --host 0.0.0.0 --port 8766
```

Then open `http://192.168.1.1:8766/` from a client that can reach that host.
Replace `192.168.1.1` with the host's real LAN address.

To bind only one interface, use that address instead:

```bash
uv run herdr-web --host 192.168.1.1 --port 8766
```

The default bind address is `127.0.0.1` for safety. The service has no separate
login. Anyone who can reach the service can control the selected Herdr session.
Only use a LAN bind on a trusted network, or place the service behind an
authenticated reverse proxy.

## Run beside Jupyter

The same process supports direct access and Jupyter Server Proxy access. If the
Jupyter server runs on the same host, bind to `0.0.0.0` when you need both LAN
access and the Jupyter proxy:

```bash
uv run herdr-web --host 0.0.0.0 --port 8766
```

Open it through Jupyter Server Proxy:

```text
https://JUPYTER_HOST/USER_BASE/proxy/8766/
```

For example, a JupyterHub user server commonly uses:

```text
https://hub.example.org/user/alice/proxy/8766/

If only Jupyter access is needed, bind to `127.0.0.1` instead. A process bound
only to `192.168.1.1` accepts direct connections to that address, but a proxy
that connects to `127.0.0.1` cannot reach it. The application keeps the
Jupyter proxy prefix when the proxy sends `X-Forwarded-Prefix`, while direct
access uses the root path.

The page loads xterm.js and its fit addon from jsDelivr. For an air-gapped
installation, serve matching xterm assets locally and replace the two CDN URLs
in `herdr_web/static/index.html`.

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
| `--port PORT` | Port to bind. Defaults to `8766`. |
| `HERDR_BINARY` | Absolute path to the Herdr binary used for the local client. Defaults to `herdr` on `PATH`. |
| `HERDR_WEB_CONFIG_DIR` | Herdr configuration directory to scan. Defaults to `~/.config/herdr`, respecting `XDG_CONFIG_HOME`. |

Only sockets found under the selected config directory are attachable. The web
client deliberately has no endpoint that accepts an arbitrary Unix-socket path.

## Current scope

This first vertical slice provides backend selection and an interactive,
terminal-faithful Herdr view. It intentionally uses the shipped Herdr client
rather than duplicating its unstable bincode client protocol in JavaScript.

Not yet implemented: offline xterm assets, multiple browser clients
coordinated for one backend, or discovery of Herdr sessions outside the
conventional config directory.
