# herdr-web

`herdr-web` is a local browser terminal for existing Herdr sessions. It is a
companion service, not a fork of Herdr and does not expose the private Herdr
socket to JavaScript.

It discovers the local user's conventional client sockets:

- `~/.config/herdr/herdr-client.sock` (the default session)
- `~/.config/herdr/sessions/*/herdr-client.sock` (named sessions)

When a user selects a session, the service starts the installed `herdr client`
in a private pseudo-terminal attached to that exact socket. The existing Herdr
client negotiates `TerminalAnsi` rendering, so the browser receives the same
server-owned TUI layout, colors, keyboard behavior, mouse protocol, panes, and
session capabilities as a terminal client. The browser is only a terminal
emulator.

## Install

Requires Python 3.10 or later and an installed `herdr` client:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run beside Jupyter

Run this as the same Unix user that owns the Herdr server and sockets:

```bash
cd /path/to/herdr-web
python -m uvicorn herdr_web.app:app --host 127.0.0.1 --port 8765
```

Open it through Jupyter Server Proxy:

```text
https://JUPYTER_HOST/USER_BASE/proxy/8765/
```

For example, a JupyterHub user server commonly uses:

```text
https://hub.example.org/user/alice/proxy/8765/
```

Keep the service bound to `127.0.0.1`; Jupyter's authenticated proxy is the
public boundary. Do not expose this port directly to a network because anyone
who reaches it can control the selected Herdr session.

The page loads xterm.js and its fit addon from jsDelivr. For an air-gapped
installation, serve matching xterm assets locally and replace the two CDN URLs
in `static/index.html`.

## Configuration

| Variable | Purpose |
| --- | --- |
| `HERDR_BINARY` | Absolute path to the Herdr binary used for the local client. Defaults to `herdr` on `PATH`. |
| `HERDR_WEB_CONFIG_DIR` | Herdr configuration directory to scan. Defaults to `~/.config/herdr`, respecting `XDG_CONFIG_HOME`. |

Only sockets found under the selected config directory are attachable. The web
client deliberately has no endpoint that accepts an arbitrary Unix-socket path.

## Current scope

This first vertical slice provides backend selection and an interactive,
terminal-faithful Herdr view. It intentionally uses the shipped Herdr client
rather than duplicating its unstable bincode client protocol in JavaScript.

Not yet implemented: browser clipboard-image bridging, offline xterm assets,
multiple browser clients coordinated for one backend, or discovery of Herdr
sessions outside the conventional config directory.
