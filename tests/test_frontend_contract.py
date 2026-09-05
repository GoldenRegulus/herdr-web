from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIRECTORY = PROJECT_ROOT / "herdr_web" / "static"


class FrontendContractTests(unittest.TestCase):
    def test_terminal_output_uses_supported_xterm_write_queue(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("_core.writeSync", application)
        self.assertIn("return new Promise((resolve) =>", application)
        self.assertIn("activeTerminal.write(bytes, () =>", application)
        self.assertIn("noteParsedOutput(flow, bytes.length)", application)
        self.assertIn("await queueTerminalOutput(base64ToBytes(message.data_base64))", application)

    def test_mouse_motion_is_coalesced_without_delaying_keys(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")

        self.assertIn("isDisposableMouseMotion(data)", application)
        self.assertIn("pendingMouseMotion = data", application)
        self.assertIn("queuePendingMouseMotion();\n      inputBuffer.append(data)", application)
        self.assertIn("MOUSE_MOTION_INTERVAL_MS = 16", application)

    def test_full_and_panes_share_auth_aware_reconnect(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        recovery_start = application.index("  async function recoverWebSocket(backend, expectedMode)")
        recovery_end = application.index("  function scheduleWebSocketReconnect(backend)", recovery_start)
        recovery = application[recovery_start:recovery_end]
        full_start = application.index("  function startWebSocket(backend)")
        full_end = application.index("  function attachFull(backend)", full_start)
        full_connection = application[full_start:full_end]

        self.assertIn("recoverWebSocket(backend, 'panes')", application)
        self.assertIn("recoverWebSocket(backend, 'full')", application)
        self.assertNotIn("recoverPaneWebSocket", application)
        self.assertIn("response.type === 'opaqueredirect'", recovery)
        self.assertIn("requiresAuthentication(response)", recovery)
        self.assertIn("expectedMode === 'full' && httpReachable", recovery)
        self.assertIn("FULL_WEBSOCKET_RETRIES_BEFORE_HTTP_FALLBACK = 2", application)
        self.assertNotIn("startHttpFallback(backend)", full_connection)
        self.assertIn("if (socket === nextSocket && !opened) nextSocket.close()", full_connection)
        self.assertIn("if (attached && !sessionId) scheduleWebSocketReconnect(backend)", full_connection)
        self.assertIn("scheduleWebSocketReconnect(backend);", application)

    def test_webgl_renderer_has_dom_fallback(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        vendor = STATIC_DIRECTORY / "vendor"

        self.assertIn("import './vendor/xterm-addon-webgl.js'", application)
        self.assertIn("activeTerminal.loadAddon(addon)", application)
        self.assertIn("addon.onContextLoss", application)
        self.assertIn("addon?.dispose()", application)
        self.assertTrue((vendor / "xterm-addon-webgl.js").is_file())
        self.assertTrue((vendor / "xterm-addon-webgl.LICENSE").is_file())

    def test_desktop_header_keeps_navigation_left_and_modes_right(self) -> None:
        document = (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")
        stylesheet = (STATIC_DIRECTORY / "style.css").read_text(encoding="utf-8")

        self.assertLess(document.index('id="backend-name"'), document.index('id="back"'))
        self.assertLess(document.index('id="back"'), document.index('id="view-modes"'))
        self.assertLess(document.index('id="view-modes"'), document.index('id="telemetry"'))
        self.assertIn("#view-modes { display: flex; flex: 0 0 auto;", stylesheet)
        self.assertIn("margin-left: auto;", stylesheet)
        self.assertIn("#back {", stylesheet)
        self.assertIn("flex: 0 0 auto;", stylesheet)
        self.assertNotIn("#back { margin-left: auto; }", stylesheet)

    def test_desktop_panes_follow_the_full_navigation_frame(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        document = (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")
        stylesheet = (STATIC_DIRECTORY / "style.css").read_text(encoding="utf-8")

        sidebar = document.index('id="pane-sidebar"')
        spaces = document.index('id="pane-workspaces"', sidebar)
        agents = document.index('id="pane-agents"', spaces)
        main = document.index('id="pane-main"', agents)
        toggle = document.index('id="pane-sidebar-toggle"', main)
        tabs = document.index('id="pane-tabs"', toggle)
        grid = document.index('id="pane-grid"', tabs)
        self.assertLess(sidebar, spaces)
        self.assertLess(spaces, agents)
        self.assertLess(agents, main)
        self.assertLess(main, toggle)
        self.assertLess(toggle, tabs)
        self.assertLess(tabs, grid)
        self.assertNotIn('id="pane-workspace"', document)
        self.assertIn("function desktopPaneNavigationItem(", application)
        self.assertGreaterEqual(
            application.count("button.dataset.status = statusValue"), 2
        )
        self.assertIn("status.className = 'visually-hidden'", application)
        self.assertNotIn("pane-sidebar-item-status", application)
        self.assertIn("paneWorkspaces.replaceChildren()", application)
        self.assertIn("paneAgents.replaceChildren()", application)
        self.assertIn("DESKTOP_PANE_SIDEBAR_COLLAPSED_KEY", application)
        self.assertIn("function setDesktopPaneSidebarCollapsed(collapsed)", application)
        self.assertIn("panesView.dataset.sidebarCollapsed", application)
        self.assertIn("setDesktopPaneSidebarCollapsed(!desktopPaneSidebarCollapsed)", application)
        self.assertIn("() => selectPaneWorkspace(workspace.workspace_id)", application)
        self.assertIn("() => selectPaneTarget(agent.pane_id)", application)
        navigation_start = stylesheet.index("#panes-view {")
        navigation_end = stylesheet.index("#pane-grid {", navigation_start)
        navigation_style = stylesheet[navigation_start:navigation_end]
        self.assertIn(
            "grid-template-columns: clamp(13rem, 18vw, 17rem) minmax(0, 1fr);",
            stylesheet,
        )
        self.assertIn("grid-template-rows: repeat(2, minmax(0, 1fr));", navigation_style)
        self.assertIn("#pane-main {", navigation_style)
        self.assertIn("grid-template-rows: 2.35rem minmax(0, 1fr);", navigation_style)
        self.assertIn('#panes-view[data-sidebar-collapsed="true"]', navigation_style)
        self.assertIn('#pane-sidebar-toggle {', navigation_style)
        self.assertIn("height: 1.75rem;", navigation_style)
        self.assertIn("min-width: 7rem;", navigation_style)
        self.assertIn("padding: .35rem 0 .45rem;", navigation_style)
        self.assertIn("border-radius: 0;", navigation_style)
        self.assertIn("border-right: 3px solid transparent;", navigation_style)
        self.assertIn("box-shadow: inset 1px 0 var(--theme-accent);", navigation_style)
        self.assertIn("box-shadow: inset 3px 0 var(--theme-accent);", navigation_style)
        self.assertNotIn('.pane-sidebar-item::before', navigation_style)
        self.assertNotIn('.pane-sidebar-item-status', navigation_style)
        working_start = navigation_style.index(
            '.pane-sidebar-item[data-status="working"]'
        )
        working_end = navigation_style.index(
            '.pane-sidebar-item[data-status="done"]', working_start
        )
        working_style = navigation_style[working_start:working_end]
        self.assertIn('@keyframes working-status-edge-pulse', navigation_style)
        self.assertIn(
            'animation: working-status-edge-pulse 1.4s ease-in-out infinite;',
            working_style,
        )
        self.assertNotIn('background:', working_style)
        self.assertIn(
            '.pane-sidebar-item[data-status="done"] { border-right-color: var(--theme-green); }',
            navigation_style,
        )
        self.assertIn(
            '.pane-sidebar-item[data-status="blocked"] { border-right-color: var(--theme-yellow); }',
            navigation_style,
        )
        self.assertIn(
            '.pane-sidebar-item[data-status="error"] { border-right-color: var(--theme-red); }',
            navigation_style,
        )
        self.assertNotIn('.pane-sidebar-item[data-status="idle"]', navigation_style)
        self.assertNotIn('--theme-mauve', stylesheet)
        self.assertNotIn('.agent-status[data-status=', stylesheet)
        self.assertGreaterEqual(
            stylesheet.count('border-right: 3px solid transparent;'), 2
        )
        self.assertIn('.sheet-item[data-status="working"]', stylesheet)
        self.assertIn(
            '.sheet-item[data-status="done"] { border-right-color: var(--theme-green); }',
            stylesheet,
        )
        self.assertIn(
            '.sheet-item[data-status="blocked"] { border-right-color: var(--theme-yellow); }',
            stylesheet,
        )
        self.assertIn('animation: none;', stylesheet)
        self.assertIn('border-right-color: var(--theme-overlay-1);', stylesheet)
        self.assertIn('.visually-hidden {', stylesheet)
        self.assertIn('#pane-sidebar { display: none; }', stylesheet)
        tab_start = navigation_style.index('#pane-tabs button {')
        selected_tab_start = navigation_style.index(
            '#pane-tabs button[aria-selected="true"]', tab_start
        )
        tab_style = navigation_style[tab_start:selected_tab_start]
        selected_tab_style = navigation_style[selected_tab_start:]
        self.assertIn('background: transparent;', tab_style)
        self.assertIn('border-bottom: 2px solid transparent;', tab_style)
        self.assertIn('border-radius: 0;', tab_style)
        self.assertIn('background: transparent;', selected_tab_style)
        self.assertIn('border-bottom-color: var(--theme-accent);', selected_tab_style)
        self.assertIn('color: inherit;', selected_tab_style)
        self.assertNotIn('background: var(--theme-surface-1);', selected_tab_style)
        self.assertNotIn("Herdr MesloLGS NF", navigation_style)
        self.assertNotIn("--terminal-column-24", stylesheet)

    def test_desktop_panes_keep_xterm_focus_after_output(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        helper_start = application.index("  function syncPaneKeyboardHelper(pane)")
        helper_end = application.index("  function focusPaneKeyboard(pane)", helper_start)
        helper_sync = application[helper_start:helper_end]

        self.assertIn("if (!mobileTarget) return;", helper_sync)
        self.assertLess(
            helper_sync.index("if (!mobileTarget) return;"),
            helper_sync.index("helper.blur();"),
        )
        self.assertIn(
            "if (!mobileQuery.matches) paneTerminals.get(streamId)?.terminal.focus();",
            application,
        )

    def test_terminal_chrome_uses_the_measured_cell_grid(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        stylesheet = (STATIC_DIRECTORY / "style.css").read_text(encoding="utf-8")

        self.assertIn("function syncTerminalGridMetrics(activeTerminal)", application)
        self.assertIn("bounds.width / activeTerminal.cols", application)
        self.assertIn("bounds.height / activeTerminal.rows", application)
        self.assertIn("fitPaneTerminal(record);", application)
        self.assertIn("scrollback: 0", application)
        self.assertIn("--terminal-cell-width", stylesheet)
        self.assertIn("--terminal-cell-height", stylesheet)
        self.assertIn("height: var(--terminal-row-3);", stylesheet)
        self.assertIn(".xterm .xterm-viewport { background-color: var(--theme-background); }", stylesheet)
        self.assertIn(".pane-terminal .xterm-viewport { overflow-y: hidden; }", stylesheet)

    def test_mobile_navigation_uses_structured_backend_api(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        document = (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")
        stylesheet = (STATIC_DIRECTORY / "style.css").read_text(encoding="utf-8")

        self.assertIn("backendApiUrl(backend, 'navigation')", application)
        self.assertIn("backendApiUrl(currentBackend, 'focus')", application)
        self.assertIn('id="mobile-toolbar"', document)
        self.assertIn('data-sheet="spaces"', document)
        self.assertIn('data-sheet="tabs"', document)
        self.assertIn('data-sheet="agents"', document)
        self.assertIn('data-sheet="more"', document)
        self.assertIn("env(safe-area-inset-bottom)", stylesheet)
        self.assertIn("@media (max-width: 700px)", stylesheet)

    def test_mobile_sheet_uses_an_expandable_flat_animated_tree(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        document = (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")
        stylesheet = (STATIC_DIRECTORY / "style.css").read_text(encoding="utf-8")

        self.assertIn('class="sheet-close-icon"', document)
        self.assertIn('id="sheet-sessions"', document)
        self.assertIn('class="sheet-sessions-icon" viewBox="6 4 12 16"', document)
        self.assertIn('<path d="m15 18-6-6 6-6"></path>', document)
        self.assertNotIn('aria-label="Close navigation">Close</button>', document)
        self.assertIn("tree.className = 'sheet-tree'", application)
        self.assertIn("function togglePaneBrowseBranch(kind, targetId)", application)
        self.assertIn("function paneBrowseChevronIcon()", application)
        self.assertIn("document.createElementNS(namespace, 'svg')", application)
        self.assertIn("icon.setAttribute('viewBox', '6 4 12 16')", application)
        self.assertIn("path.setAttribute('d', 'm9 18 6-6-6-6')", application)
        self.assertIn("paneBrowseExpandedWorkspaces", application)
        self.assertIn("paneBrowseExpandedTabs", application)
        self.assertIn("tabPanes.length > 1 && tabExpanded", application)
        self.assertIn("? () => togglePaneBrowseBranch('tab', tab.tab_id)", application)
        self.assertIn(": () => selectPaneTab(tab.tab_id)", application)
        self.assertNotIn("const tabDetail", application)
        self.assertNotIn("const paneDetail", application)
        self.assertIn("sheetSessions.addEventListener('click', () => showPicker())", application)
        self.assertIn("MIN_MOBILE_TERMINAL_FONT_SIZE = 10", application)
        self.assertIn("MAX_MOBILE_TERMINAL_FONT_SIZE = 24", application)
        self.assertIn("mobileSheet.dataset.state = 'closing'", application)
        self.assertIn("mobileSheet.dataset.state = animate ? 'opening' : 'open'", application)
        self.assertIn("MOBILE_SHEET_ANIMATION_MS = 240", application)
        self.assertIn(".sheet-item {", stylesheet)
        self.assertIn("border-radius: 0;", stylesheet)
        self.assertIn(".sheet-tree-branch[aria-expanded=\"true\"] .sheet-tree-chevron", stylesheet)
        self.assertIn(".sheet-tree-chevron {", stylesheet)
        self.assertIn("flex: 0 0 .875rem;", stylesheet)
        self.assertIn("transform: translateY(1px) rotate(90deg);", stylesheet)
        self.assertNotIn(".sheet-item-label::before", stylesheet)
        self.assertIn("nameText.className = 'sheet-item-label-text'", application)
        self.assertIn("--sheet-child-item-height: max(40px", stylesheet)
        self.assertIn("padding-left: var(--sheet-tree-indent)", stylesheet)
        self.assertIn(".sheet-tree .sheet-item { box-shadow: inset 1px 0 var(--theme-accent); }", stylesheet)
        self.assertIn("box-shadow: inset 3px 0 var(--theme-accent);", stylesheet)
        self.assertNotIn("var(--theme-accent) 12%", stylesheet)
        self.assertNotIn("var(--theme-accent) 22%", stylesheet)
        self.assertNotIn(".sheet-tree li li::before", stylesheet)
        self.assertIn("grid-template-columns: var(--terminal-row-3) minmax(0, 1fr)", stylesheet)
        self.assertIn('#mobile-sheet[data-state="open"] > section', stylesheet)
        self.assertIn("transform: translateY(calc(100% + 1px))", stylesheet)
        self.assertIn("@media (prefers-reduced-motion: reduce)", stylesheet)

    def test_mobile_uses_panes_with_touch_scrolling(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        document = (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")
        stylesheet = (STATIC_DIRECTORY / "style.css").read_text(encoding="utf-8")
        mobile_input = (STATIC_DIRECTORY / "mobile-prediction.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("if (mobileQuery.matches) return 'panes'", application)
        self.assertNotIn("Open Full mode", application)
        self.assertIn("addEventListener('touchmove'", application)
        self.assertIn("touchScrollDistance > 0 ? 'up' : 'down'", application)
        self.assertIn("Math.exp(-elapsed / MOBILE_SCROLL_DECAY_MS)", application)
        self.assertIn("MOBILE_SCROLL_DECAY_MS = 240", application)
        self.assertIn("requestAnimationFrame(step)", application)
        self.assertIn("reducedMotionQuery.matches", application)
        self.assertIn("].join(' · ')", application)
        self.assertNotIn('id="pane-breadcrumb"', document)
        self.assertNotIn('id="pane-connection"', document)
        self.assertIn('d="M4 6h16M4 12h16M4 18h16"', document)
        self.assertIn('id="mobile-primary-row"', document)
        self.assertIn('id="mobile-default-row"', document)
        self.assertIn('id="mobile-navigation-row"', document)
        self.assertIn('id="mobile-modifiers"', document)
        self.assertIn('data-modifier="control"', document)
        self.assertIn('data-modifier="alt"', document)
        self.assertIn('data-modifier="shift"', document)
        self.assertIn('data-terminal-key="escape"', document)
        self.assertIn('data-terminal-key="tab"', document)
        self.assertIn('id="mobile-navigation-mode"', document)
        self.assertIn('id="mobile-snapshot"', document)
        for key in ("home", "page-up", "page-down", "end", "delete"):
            self.assertIn(f'data-navigation-key="{key}"', document)
        self.assertIn('id="mobile-mouse-mode"', document)
        self.assertIn('aria-pressed="false" data-mode="off"', document)
        self.assertIn('data-mode-icon="on"', document)
        self.assertIn('data-mode-icon="off"', document)
        self.assertIn('class="mobile-bar-icon mobile-pointer-icon"', document)
        self.assertIn('M4.037 4.688a.495.495', document)
        self.assertIn('M22 2 2 22', document)
        self.assertTrue((STATIC_DIRECTORY / "icons" / "LUCIDE.LICENSE").is_file())
        self.assertNotIn("Clipboard mode", document)
        self.assertIn('id="mobile-keyboard-lock"', document)
        self.assertIn('data-lock-icon="locked"', document)
        self.assertIn('data-lock-icon="unlocked"', document)
        for direction in ("left", "up", "down", "right"):
            self.assertIn(f'data-arrow="{direction}"', document)
        self.assertIn("function applyMobileModifiers(data)", application)
        self.assertIn("if (data === '\\r') return terminalDataForModifiedEnter", application)
        self.assertIn("function physicalModifiedEnterData(event)", application)
        self.assertIn("function sendMobileArrow(direction)", application)
        self.assertIn("function sendMobileTerminalKey(key)", application)
        self.assertIn("function sendMobileNavigationKey(key)", application)
        self.assertIn("terminalDataForNavigationKey(key, mobileModifierState)", application)
        self.assertIn("mobileKeyRepeatInterval = setInterval(send, 70)", application)
        self.assertIn("let retainedKeyboardPane", application)
        self.assertIn("button.addEventListener('touchstart'", application)
        self.assertIn("if (event.pointerType !== 'touch')", application)
        self.assertIn("if (event.cancelable) event.preventDefault()", application)
        self.assertIn("button.addEventListener('mousedown', (event) => event.preventDefault())", application)
        self.assertIn("if (pane && pane === selectedPaneTerminal()) focusPaneKeyboard(pane)", application)
        self.assertIn("function terminalZoomControls()", application)
        self.assertIn("'herdr-web-mobile-terminal-font-size'", application)
        self.assertIn("fontSize: paneCompact ? mobileTerminalFontSize", application)
        self.assertIn("pane.terminal.options.fontSize = nextSize", application)
        self.assertIn("sheetHeading('Terminal size'), terminalZoomControls()", application)
        self.assertNotIn("function renderClipboardSheet()", application)
        self.assertNotIn("Copy selection or screen", application)
        self.assertNotIn("Paste text…", application)
        self.assertNotIn("document.createElement('textarea')", application)
        self.assertIn("function fitPaneTerminal(pane)", application)
        self.assertIn("--pane-terminal-remainder", application)
        self.assertIn("function sendTerminalMouseClick(pane, button, clientX, clientY)", application)
        self.assertIn("function sendTerminalMouseReport(pane, button, clientX, clientY", application)
        self.assertIn("function queuePaneMouseClick(pane, buttonCode, point)", application)
        self.assertIn("kind: 'pane-mouse'", application)
        self.assertIn("type: 'pane-mouse'", application)
        self.assertIn("action: 'click'", application)
        self.assertIn("MOBILE_LONG_PRESS_MS = 500", application)
        self.assertIn("MOBILE_MOUSE_DRAG_HOLD_MS = 180", application)
        self.assertIn("PANE_SCROLL_FLUSH_MS = 50", application)
        self.assertIn("PANE_SCROLL_FRAME_TIMEOUT_MS = 120", application)
        self.assertIn("let mobileMouseMode = false", application)
        self.assertIn("mobileMouseMode = false;", application)
        self.assertIn("touchMouseGesture = held < MOBILE_MOUSE_DRAG_HOLD_MS", application)
        self.assertIn("function flushPaneScroll(pane, force = false)", application)
        self.assertIn("if (mobileMouseMode && touchMouseGesture", application)
        self.assertIn("'Mouse input off; Paste is available'", application)
        self.assertNotIn("Clipboard mode", application)
        self.assertNotIn("MOBILE_CLIPBOARD_SCROLL_MOVE_PX", application)
        self.assertNotIn("classifyClipboardTouchMove(", application)
        self.assertNotIn("clipboardTouchGesture", application)
        self.assertIn("rightClickSelectsWord: false", application)
        self.assertNotIn("function terminalSelectionPoint(pane, clientX, clientY)", application)
        self.assertNotIn("function selectTerminalTouchRange(pane, start, end)", application)
        self.assertNotIn("function copyTerminalSelection(pane)", application)
        self.assertNotIn("ownerDocument.execCommand('copy')", application)
        self.assertIn("function queuePaneTextPaste(pane, text)", application)
        self.assertIn("normalizeTerminalPasteText(text)", application)
        self.assertIn("kind: 'pane-paste'", application)
        self.assertIn("type: 'pane-paste'", application)
        self.assertIn("event.inputType === 'insertFromPaste'", application)
        self.assertIn("Turn mouse input off to Paste", application)
        self.assertIn("sendClipboardImage(image, record)", application)
        self.assertIn("stream_id: operation.streamId", application)
        self.assertNotIn("Herdr's pane API has no image input", application)
        self.assertNotIn("sendTerminalMouseClick(record, 0, event.clientX, event.clientY)", application)
        self.assertNotIn("clipboardOverlay", application)
        self.assertNotIn("terminal-native-clipboard", stylesheet)
        self.assertIn("ownerDocument.getSelection()", application)
        self.assertNotIn("cursorInput = document.createElement('input')", application)
        self.assertIn("function syncPaneKeyboardHelper(pane)", application)
        self.assertIn("function focusPaneKeyboard(pane)", application)
        self.assertIn("function handleMobileTextInput(pane, event)", application)
        self.assertIn("applyMobileTextValue(pane, helper.value, mobileHelperCaret(helper)", application)
        self.assertIn("Do not also let xterm", application)
        self.assertIn("MOBILE_BACKSPACE_RESET_MS", application)
        self.assertIn("terminalDataForRepeatedMobileBackspace", application)
        self.assertIn("pane.terminal.buffer.active.cursorX", application)
        self.assertIn("pane.terminal.buffer.active.cursorY", application)
        self.assertIn("sendTerminalMouseClick(record, 0, touchPointerX, touchPointerY);", application)
        self.assertIn("focusPaneKeyboard(record);", application)
        self.assertIn("touchDuration < MOBILE_LONG_PRESS_MS", application)
        self.assertIn("if (mobileKeyboardLocked || pane.mode", application)
        self.assertIn("function setMobileKeyboardLocked(locked", application)
        self.assertIn("if (!paneCompact) selectedRecord.terminal.focus()", application)
        self.assertIn("function focusTerminalAfterControl()", application)
        self.assertIn("dataset.mobileKeyboard !== 'open'", application)
        self.assertIn("const keyboardFocused = document.activeElement === paneKeyboardHelper(pane)", application)
        self.assertIn("focusPaneKeyboard(pane);", application)
        self.assertIn("function syncVisualViewportLayout()", application)
        self.assertIn("MOBILE_KEYBOARD_MINIMUM_SHRINK_PX = 120", application)
        self.assertIn("root.dataset.mobileKeyboard = keyboardOpen ? 'open' : 'closed'", application)
        self.assertIn("window.visualViewport?.addEventListener('resize'", application)
        self.assertIn("--visual-viewport-height", stylesheet)
        self.assertIn("--mobile-control-bottom-clearance", stylesheet)
        self.assertIn(':root[data-mobile-keyboard="closed"]', stylesheet)
        self.assertIn(':root[data-mobile-keyboard="open"]', stylesheet)
        self.assertIn("left: calc(var(--visual-viewport-left) + .5rem);", stylesheet)
        self.assertIn("max-width: calc(var(--visual-viewport-width) - 1rem);", stylesheet)
        self.assertIn("var(--visual-viewport-top) + env(safe-area-inset-top)", stylesheet)
        self.assertIn("#web-toasts {", stylesheet)
        self.assertIn("pointer-events: none;", stylesheet)
        self.assertIn(".web-toast:not(.clipboard-retry)", application)
        self.assertIn("const IOS_KEYBOARD_FONT_SIZE = 16", application)
        self.assertIn("MOBILE_NATIVE_MENU_CLICK_SUPPRESSION_MS = 750", application)
        self.assertIn("const targetWidth = nativePasteTarget ? screenBounds.width : 1", application)
        self.assertIn("const targetHeight = Math.max(1, cellHeight)", application)
        self.assertIn("const targetLeft = nativePasteTarget ? screenLeft : cursorLeft", application)
        self.assertIn("'mobile-native-paste-target', nativePasteTarget", application)
        self.assertIn("suppressClipboardClickUntil", application)
        self.assertIn("preserveClipboardFocusUntil", application)
        self.assertIn("event.button === 2 || performance.now() < preserveClipboardFocusUntil", application)
        self.assertIn(".xterm-helper-textarea.mobile-keyboard-target", stylesheet)
        self.assertIn(".mobile-native-paste-target", stylesheet)
        self.assertIn("opacity: 0;", stylesheet)
        self.assertIn("opacity: 1;", stylesheet)
        self.assertIn("pointer-events: auto;", stylesheet)
        self.assertIn("box-shadow: none;", stylesheet)
        self.assertNotIn(".terminal-cursor-input", stylesheet)
        self.assertNotIn("terminal-selection-actions", document)
        self.assertIn('aria-label="Back to sessions"', document)
        self.assertIn("function syncPaneConnectionStatus()", application)
        self.assertIn("pane.connection.disabled = !canReconnect", application)
        self.assertIn("function requestReconnect()", application)
        self.assertIn("function requiresAuthentication(response)", application)
        self.assertIn("function beginAuthenticationReload()", application)
        self.assertIn("function reloadForAuthentication(response)", application)
        self.assertIn("function recoverWebSocket(backend, expectedMode)", application)
        self.assertIn("response.type === 'opaqueredirect'", application)
        self.assertIn("if (reloadForAuthentication(response))", application)
        self.assertIn("setStatus('Authentication expired', 'disconnected')", application)
        self.assertIn("connection.className = 'pane-connection'", application)
        self.assertNotIn("reconnect.textContent = 'Reconnect terminal'", application)
        self.assertIn("button:focus-visible { background: var(--theme-surface-1); outline: none; }", stylesheet)
        self.assertIn("@media (hover: hover) and (pointer: fine)", stylesheet)
        self.assertIn("button:enabled:hover { background: var(--theme-surface-1); outline: none; }", stylesheet)
        self.assertNotIn("button:hover, button:focus-visible", stylesheet)
        self.assertIn("-webkit-touch-callout: none;", stylesheet)
        self.assertIn("touch-action: none;", stylesheet)
        self.assertIn("#pane-grid .pane-terminal .xterm-rows", stylesheet)
        self.assertIn("-webkit-user-select: none !important;", stylesheet)
        self.assertIn(".pane-snapshot-view .xterm-rows", stylesheet)
        self.assertIn("-webkit-user-select: text !important;", stylesheet)
        self.assertIn("function captureTerminalSnapshot(source)", application)
        self.assertIn("function openTerminalSnapshot(pane)", application)
        self.assertIn("function closeTerminalSnapshot(pane, syncControls = true)", application)
        self.assertIn("function renderMobileSnapshotButton(active)", application)
        self.assertNotIn("snapshotButton.className = 'pane-snapshot'", application)
        self.assertIn("disableStdin: true", application)
        self.assertIn("snapshotTerminal.write(capture.ansi", application)
        self.assertIn("pane.snapshot = { terminal: snapshotTerminal, view }", application)
        self.assertIn("pane?.snapshot) return undefined", application)
        self.assertIn("selectedPaneTerminal()?.snapshot", application)
        self.assertIn(".pane-snapshot-view .xterm-rows", stylesheet)
        self.assertIn("--terminal-row-4: 72px", stylesheet)
        self.assertIn("grid-template-rows: repeat(2, var(--terminal-row-2))", stylesheet)
        self.assertIn("touch-action: manipulation;", stylesheet)

    def test_panes_mode_uses_structured_layout_and_one_multiplexed_socket(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        document = (STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="panes-mode"', document)
        self.assertIn('id="pane-grid"', document)
        self.assertIn('id="pane-browse"', document)
        self.assertIn("type: 'panes.attach'", application)
        self.assertIn("navigationSnapshot?.layouts", application)
        self.assertIn("PANE_FRAME_MAGIC", application)
        self.assertIn("PANE_FRAME_FLAG_DEFLATE", application)
        self.assertIn("paneDeflateSupported", application)
        self.assertIn("function decompressPaneFrame(frame)", application)
        self.assertIn("new DecompressionStream('deflate')", application)
        self.assertIn("const reader = stream.getReader()", application)
        self.assertIn("if (size > MAX_PANE_FRAME_BYTES)", application)
        self.assertIn("await reader.cancel('decompressed pane frame is too large')", application)
        self.assertIn("frameChain: Promise.resolve()", application)
        self.assertIn("compression: paneDeflateSupported ? 'deflate' : undefined", application)
        self.assertIn("function acknowledgePaneFrame(frame, flow)", application)
        self.assertIn("frame.width === pane.terminal.cols", application)
        self.assertIn("frame.height === pane.terminal.rows", application)
        self.assertIn("pane.awaitingFull && !frame.full", application)
        self.assertIn("pane.awaitingFull = true;", application)
        self.assertIn("stream_id: pane.streamId", application)
        self.assertIn("Queued input was discarded because this pane is read-only", application)
        self.assertIn("pendingPaneActivation = streamId", application)
        self.assertIn("function refreshPaneTitles()", application)
        self.assertIn("refreshPaneTitles();", application)
        self.assertIn("record?.agent_status", application)
        self.assertIn("function paneStructureFingerprint(snapshot, tabId)", application)
        self.assertIn("function schedulePaneStructureRebuild()", application)
        self.assertIn("inputBuffer.length || inputOperations.length || pendingMouseMotion", application)
        self.assertIn("function paneAcceptsInput(pane, announce = true)", application)
        self.assertIn("pane.terminal.options.disableStdin = true", application)
        self.assertIn("pane.mode === 'control' && !pane.closed", application)

    def test_mobile_input_uses_only_a_bounded_browser_owned_suffix(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        prediction = (STATIC_DIRECTORY / "mobile-prediction.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("function handleMobileTextInput(pane, event)", application)
        self.assertIn("pane.mobilePredictionText, text, pane.mobilePredictionCursor, cursor", application)
        self.assertIn("MOBILE_PREDICTION_TEXT_LIMIT = 1024", prediction)
        self.assertIn("terminalHasEditableText(pane.terminal, text)", application)
        self.assertIn("event.inputType === 'insertReplacementText'", application)
        self.assertIn("event.inputType !== 'insertReplacementText'", application)
        self.assertIn("discard the first swipe insertion", application)
        self.assertIn("if (!edit)", application)
        self.assertIn("if (input)", application)
        self.assertIn("Do not also let xterm", application)
        self.assertIn("event.type === 'keydown' || event.type === 'keypress'", application)
        self.assertIn("function resetMobilePaneInput(pane, blur = false)", application)
        self.assertNotIn("mobilePredictionSynthetic", application)
        self.assertIn("export function terminalTextInputDelta(", prediction)
        self.assertNotIn("minimumMatch", prediction)
        self.assertNotIn("._core", prediction)
        self.assertNotIn("._core", application)

    def test_flat_controls_keep_mobile_terminal_space(self) -> None:
        stylesheet = (STATIC_DIRECTORY / "style.css").read_text(encoding="utf-8")
        button = stylesheet.split("\nbutton {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 0;", button)
        header = stylesheet.split("#terminal-view > header {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size: .875rem;", header)
        self.assertIn("height: 40px;", header)
        for selector in ("#view-modes button {", "#back {", "#telemetry {"):
            control = stylesheet.split(selector, 1)[1].split("}", 1)[0]
            self.assertIn("height: 28px;", control)
        mobile = stylesheet.split("#pane-mobile-bar button {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size: .75rem;", mobile)
        self.assertIn("height: calc(var(--terminal-row-2) - 2px);", mobile)
        telemetry = stylesheet.split("#telemetry {", 1)[1].split("}", 1)[0]
        self.assertIn("background: transparent;", telemetry)
        self.assertIn("border: 1px solid transparent;", telemetry)
        self.assertIn('border-color: var(--theme-red);', stylesheet)
        self.assertIn("button:disabled { cursor: default;", stylesheet)
        self.assertIn('[aria-busy="true"] button:disabled { cursor: wait; }', stylesheet)
        self.assertIn("border-radius: 1rem 1rem 0 0;", stylesheet)

    def test_mobile_caret_moves_the_terminal_through_ordered_input(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        self.assertIn("'selectionchange', handleMobileCaretSelection, true", application)
        self.assertIn("'select', handleMobileCaretSelection, true", application)
        self.assertIn("function followMobileCaret(pane, beforeBlur = false)", application)
        self.assertIn("followMobileCaret(pane, true)", application)
        self.assertIn("pane.mobilePredictionInvalidated ? edit.inserted : edit.data", application)
        self.assertIn("helper.value !== pane.mobilePredictionText", application)
        self.assertIn("helper.selectionStart !== helper.selectionEnd", application)
        self.assertIn("pane.mobilePredictionComposition || pane.mobileBackspaceSentinel", application)
        self.assertIn("if (sendMobilePaneKeyboardData(pane, data)) pane.mobilePredictionCursor = cursor", application)
        self.assertIn("pane.terminal.modes?.applicationCursorKeysMode", application)
        self.assertNotIn("cursor-overlay", application)

    def test_mobile_backspace_keeps_a_native_marker_without_a_repeat_timer(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")

        self.assertIn("MOBILE_BACKSPACE_SENTINEL = 'x'", application)
        self.assertIn("document.execCommand('insertText'", application)
        self.assertIn("mobileBackspaceSentinelInsertion", application)
        self.assertIn("event.inputType !== 'deleteContentBackward'", application)
        self.assertIn("terminalDataForBeforeInput(event.inputType)", application)
        self.assertIn("terminalDataForRepeatedMobileBackspace", application)
        self.assertIn("preserveMobileHelperForBackspace(pane)", application)
        self.assertNotIn("startManagedMobileBackspaceRepeat", application)

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

    def test_xterm_contains_mobile_paste_fixes(self) -> None:
        application = (STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
        stylesheet = (STATIC_DIRECTORY / "vendor" / "xterm.css").read_text(
            encoding="utf-8"
        )
        xterm = (STATIC_DIRECTORY / "vendor" / "xterm.js").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'e.clipboardData&&(e.preventDefault(),r(e.clipboardData.getData("text/plain")',
            xterm,
        )
        self.assertIn(
            '_inputEvent(e){if("insertFromPaste"===e.inputType&&this.textarea?.value)',
            xterm,
        )
        self.assertIn("return pane?.terminal.textarea;", application)
        self.assertNotIn(
            "terminal.element?.querySelector('.xterm-helper-textarea')",
            application,
        )
        self.assertIn(".xterm .xterm-width-cache-measure-container", stylesheet)
        self.assertTrue(
            (STATIC_DIRECTORY / "vendor" / "xterm.MOBILE-PASTE-PATCH.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
