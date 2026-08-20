# Synchronized-output rendering patch

The vendored xterm.js 6.0.0 file contains one local rendering patch.

Herdr wraps each `TerminalAnsi` frame in DEC private mode 2026 synchronized-output markers. xterm.js flushes the completed frame when mode 2026 closes, but version 6.0.0 defers the render to `requestAnimationFrame`. A following frame can enable mode 2026 before that render occurs. xterm.js then suppresses the completed render.

The patch renders rows immediately when `SynchronizedOutputHandler.flush()` returns buffered rows. Normal output still uses xterm's render debouncer.

This is an adaptation of:

- https://github.com/xtermjs/xterm.js/issues/6071
- https://github.com/xtermjs/xterm.js/pull/6073

Remove this patch when a released xterm.js version contains a synchronized-output fix with equivalent behavior.
