# Browser tests against real Herdr

These tests drive herdr-web in Chromium with Playwright. Each one types into
a real Herdr pane and checks what bash printed, so they test the line the
program ended up with, not only what the browser sent.

Phones are emulated: Chromium with iPhone 13 and Pixel 7 user agents, touch
and viewports. iOS keyboards are reproduced with real key events, Android
ones with input events only (keyCode 229), compositions through the
DevTools protocol. Real Safari and real keyboards can still behave
differently.

## What they cover

`suite.js`: core input.

- Desktop: Full mode, Panes mode with two panes, narrow window.
- Phones: tap versus swipe and fling for raising the keyboard, typing,
  predictive words (also right before Enter), backspace, caret moves,
  arrow and Tab keys, bracketed and plain paste, the iOS paste menu,
  backspace across a wrapped row, Android IME composition, keyboard lock.

`edge.js`: phone edge cases.

- Text typed, appended, cleared or submitted by another Herdr client.
- The keyboard dropped and raised again, with the line changed meanwhile.
- Spacebar caret swipes, overshoots, a caret moved into the prompt,
  replacing a selection.
- Tab completion, history recall, emoji, autocorrect, double-space period,
  select all and delete, word delete, Ctrl+C, rotation, switching tabs,
  fast typing.
- A multi-line prompt like an agent's (`ml.py`): newlines, joining lines,
  moving up a line, multi-line paste, a long wrapped line.
- Hold-to-copy across wrapped lines and real newlines, partial rows, a
  wrap that falls on a space, blank lines and repeated spaces.

## Requirements

- Herdr 0.9.5 or later: `curl -fsSL https://herdr.dev/install.sh | sh`
- `uv`, and Node 18 or later
- Playwright with Chromium, from this directory:

  ```bash
  npm install
  npx playwright install chromium
  ```

## Run

The tests use their own Herdr session, `herdr-web-e2e`, and their own port,
8799. Your default session and any herdr-web you already run are left
alone.

```bash
tests/e2e/setup.sh                       # a fresh herdr-web-e2e session
uv run herdr-web --port 8799 &           # from the checkout under test

cd tests/e2e
node suite.js                            # or: node suite.js ios
node edge.js                             # or: node edge.js android
HERDR_E2E_FILTER=copy node edge.js       # only cases whose name matches

./teardown.sh                            # remove the session when done
```

Rerun `setup.sh` before a run if an earlier one was interrupted: a run
leaves the panes at a prompt, but an interrupted one can leave a program
running in them.

Each failure names the check and the last lines the pane showed. Final
screenshots go to the `herdr-web-e2e-shots` folder in the system temp
directory. `HERDR_E2E_TRACE=1` prints every input frame the page sends.

## Comparing another branch

Serve a different checkout on the same port and run the same suites:

```bash
git worktree add /tmp/herdr-web-main origin/main
(cd /tmp/herdr-web-main && uv run herdr-web --port 8799) &
```

Settings: `HERDR_E2E_SESSION` names the Herdr session (default
`herdr-web-e2e`), and `HERDR_E2E_URL` the herdr-web address (default
`http://127.0.0.1:8799/`).
