# Mobile Paste patches

The vendored xterm.js 6.0.0 file contains two text Paste patches adapted from:

- https://github.com/xtermjs/xterm.js/pull/5961
- commit `91d175419ee4cc82f7001b5d433996f75fc6c44a`

The patches do the following work:

1. The clipboard Paste handler calls `preventDefault()` when `clipboardData` is available. This prevents WebKit from inserting the same text into xterm's helper textarea after xterm sends it to the terminal.
2. The input handler accepts `insertFromPaste` when native WebKit Paste first puts text in the helper textarea instead of supplying a usable clipboard event. It sends the helper value through xterm's normal `paste()` path, which applies newline and bracketed-Paste processing and then clears the helper.

Herdr Web does not include the pull request's automatic Apple-device mode. Herdr Web has an explicit Mouse switch, a keyboard lock, structured mouse clicks, and a 16 px helper font. The application enables an adapted full-row pointer target only when Mouse is off and the keyboard is unlocked. This target stays transparent and covers the complete terminal row that contains the cursor so a native long press can show Paste. A held touch suppresses its later compatibility click so it does not send an unwanted terminal click. When the helper already has focus, Herdr Web also cancels the default compatibility `mousedown` for a held touch or right-click. This keeps the software-keyboard focus while it permits the native `contextmenu` event.

The live mobile terminal does not permit text selection. Terminal Snapshot provides the stable native-selection and Copy surface.

Panes captures text Paste before xterm processes it. It normalizes CRLF and CR to LF, then sends the text through Herdr's public `pane.send_input` API. Herdr applies the server terminal's bracketed-paste state. Full mode continues to use xterm's Paste path and therefore still needs the vendored WebKit fixes.

Herdr Web keeps mobile input routing in `../app.js`, with pure suffix operations in `../mobile-prediction.js`. The xterm custom key handler rejects unmodified printable iOS key events before xterm sends them, and the application handles the matching native helper-value update once. The bounded browser-owned suffix never contains arbitrary terminal output.

Remove the JavaScript patches when a released xterm.js version contains equivalent Paste behavior. Keep the Herdr-specific Mouse, helper, and Terminal Snapshot policies unless that release provides equivalent explicit controls that pass the mobile acceptance tests.
