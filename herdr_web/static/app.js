import {
  InputByteBuffer,
  isDisposableMouseMotion,
  normalizeTerminalPasteText,
  terminalDataForBeforeInput,
  terminalDataForModifiedEnter,
  terminalDataForNavigationKey,
  terminalDataForRepeatedMobileBackspace,
} from './input-buffer.js';
import {
  MOBILE_PREDICTION_TEXT_LIMIT,
  terminalCaretInput,
  terminalHasEditableText,
  terminalPredictionReplacement,
  terminalTextInputDelta,
} from './mobile-prediction.js';
import './vendor/xterm.js';
import './vendor/xterm-addon-fit.js';
import './vendor/xterm-addon-webgl.js';

const { Terminal } = globalThis;
const { FitAddon } = globalThis.FitAddon;
const { WebglAddon } = globalThis.WebglAddon;

(async () => {
  const picker = document.querySelector('#picker');
  const terminalView = document.querySelector('#terminal-view');
  const backendList = document.querySelector('#backends');
  const pickerError = document.querySelector('#picker-error');
  const addSessionButton = document.querySelector('#add-session');
  const status = document.querySelector('#connection-status');
  const connectionIndicator = document.querySelector('#connection-indicator');
  const fps = document.querySelector('#fps');
  const telemetry = document.querySelector('#telemetry');
  const toastHost = document.querySelector('#web-toasts');
  const terminalHost = document.querySelector('#terminal');
  const panesView = document.querySelector('#panes-view');
  const paneGrid = document.querySelector('#pane-grid');
  const paneWorkspaces = document.querySelector('#pane-workspaces');
  const paneAgents = document.querySelector('#pane-agents');
  const paneSidebarToggle = document.querySelector('#pane-sidebar-toggle');
  const paneTabs = document.querySelector('#pane-tabs');
  const paneBrowse = document.querySelector('#pane-browse');
  const mobileModifiers = document.querySelector('#mobile-modifiers');
  const mobileSpecialKeys = document.querySelector('#mobile-special-keys');
  const mobileMouseModeButton = document.querySelector('#mobile-mouse-mode');
  const mobileKeyboardLockButton = document.querySelector('#mobile-keyboard-lock');
  const mobileArrows = document.querySelector('#mobile-arrows');
  const mobileNavigationModeButton = document.querySelector('#mobile-navigation-mode');
  const mobileDefaultRow = document.querySelector('#mobile-default-row');
  const mobileNavigationRow = document.querySelector('#mobile-navigation-row');
  const mobileSnapshotButton = document.querySelector('#mobile-snapshot');
  const paneMobileBar = document.querySelector('#pane-mobile-bar');
  const fullModeButton = document.querySelector('#full-mode');
  const panesModeButton = document.querySelector('#panes-mode');
  const backendName = document.querySelector('#backend-name');
  const mobileToolbar = document.querySelector('#mobile-toolbar');
  const mobileSheet = document.querySelector('#mobile-sheet');
  const mobileSheetPanel = mobileSheet.querySelector(':scope > section');
  const sheetTitle = document.querySelector('#sheet-title');
  const sheetSessions = document.querySelector('#sheet-sessions');
  const sheetContent = document.querySelector('#sheet-content');
  const mobileQuery = matchMedia('(max-width: 700px), (pointer: coarse) and (max-width: 900px)');

  let sessionId;
  let currentBackend;
  let socket;
  let connectTimer;
  let reconnectTimer;
  let reconnectStableTimer;
  let reconnectReloadTimer;
  let reconnectAttempts = 0;
  let authenticationReloading = false;
  let httpFallbackStarting = false;
  let terminal;
  let fitAddon;
  let webglAddon;
  let resizeObserver;
  let resizeQueued = false;
  let receivedFrames = 0;
  let outputFlow;
  let inputDrainTimer;
  let mouseMotionTimer;
  let pendingMouseMotion;
  let pendingPaneActivation;
  let httpInputInFlight = false;
  let httpInputReady = false;
  const inputEncoder = new TextEncoder();
  const inputBuffer = new InputByteBuffer(inputEncoder);
  const inputOperations = [];
  const INPUT_BATCH_BYTES = 16 * 1024;
  const INPUT_WEBSOCKET_HIGH_WATER_BYTES = 32 * 1024;
  const MAX_INPUT_OPERATIONS = 4096;
  const MAX_RETAINED_INPUT_OPERATION_BYTES = 16 * 1024 * 1024;
  const MAX_PANE_TEXT_PASTE_BYTES = 512 * 1024;
  const FULL_WEBSOCKET_RETRIES_BEFORE_HTTP_FALLBACK = 2;
  const OUTPUT_ACK_BATCH_BYTES = 64 * 1024;
  const OUTPUT_ACK_DELAY_MS = 10;
  const MOUSE_MOTION_INTERVAL_MS = 16;
  const MOBILE_LONG_PRESS_MS = 500;
  const MOBILE_LONG_PRESS_MOVE_PX = 10;
  const MOBILE_NATIVE_MENU_CLICK_SUPPRESSION_MS = 750;
  const MOBILE_BACKSPACE_RESET_MS = 400;
  const MOBILE_BACKSPACE_BEFORE_INPUT_SUPPRESSION_MS = 200;
  const MOBILE_BACKSPACE_SENTINEL = 'x';
  const MOBILE_MOUSE_DRAG_HOLD_MS = 180;
  const MOBILE_SCROLL_MAX_VELOCITY = 2.2;
  const MOBILE_SCROLL_DECAY_MS = 240;
  const MOBILE_SCROLL_STOP_VELOCITY = 0.04;
  const PANE_SCROLL_FLUSH_MS = 50;
  const PANE_SCROLL_FRAME_TIMEOUT_MS = 120;
  const MAX_CLIPBOARD_IMAGE_BYTES = 16 * 1024 * 1024;
  const IOS_KEYBOARD_FONT_SIZE = 16;
  const MOBILE_KEYBOARD_MINIMUM_SHRINK_PX = 120;
  const MOBILE_SHEET_ANIMATION_MS = 240;
  const DEFAULT_TERMINAL_FONT_SIZE = 14;
  const MIN_MOBILE_TERMINAL_FONT_SIZE = 10;
  const MAX_MOBILE_TERMINAL_FONT_SIZE = 24;
  const MOBILE_TERMINAL_FONT_SIZE_KEY = 'herdr-web-mobile-terminal-font-size';
  const DESKTOP_PANE_SIDEBAR_COLLAPSED_KEY = 'herdr-web-desktop-pane-sidebar-collapsed';
  const clipboardImageExtensions = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/bmp': 'bmp',
  };
  const appearanceQuery = matchMedia('(prefers-color-scheme: light)');
  const reducedMotionQuery = matchMedia('(prefers-reduced-motion: reduce)');
  const iosKeyboard = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let terminalTheme = {
    background: '#181825',
    foreground: '#cdd6f4',
    cursor: '#89b4fa',
  };
  let appliedThemeFingerprint;
  let themeSyncInFlight = false;
  let navigationSnapshot;
  let navigationFingerprint;
  let navigationTimer;
  let navigationRequest;
  let openSheetName;
  let mobileSheetCloseTimer;
  let mobileSheetOpenFrame;
  let viewMode = 'full';
  let paneViewToken = 0;
  let paneCompact = mobileQuery.matches;
  let paneResponsiveTimer;
  let paneStructureRebuildTimer;
  let visualViewportFrame;
  let mobileViewportBaselineWidth;
  let mobileViewportMaximumHeight = 0;
  let selectedWorkspace;
  let selectedTab;
  let selectedPane;
  let paneBrowseExpansionBackendId;
  const paneBrowseExpandedWorkspaces = new Set();
  const paneBrowseExpandedTabs = new Set();
  let currentPaneRequests = [];
  const paneTerminals = new Map();
  const mobileModifierState = { control: false, alt: false, shift: false };
  let mobileMouseMode = false;
  let mobileKeyboardLocked = false;
  let mobileNavigationMode = false;
  let mobileTerminalFontSize = readMobileTerminalFontSize();
  let desktopPaneSidebarCollapsed = readDesktopPaneSidebarCollapsed();
  let mobileKeyRepeatDelay;
  let mobileKeyRepeatInterval;
  const PANE_FRAME_MAGIC = 0x48575031;
  const PANE_FRAME_HEADER_BYTES = 21;
  const PANE_FRAME_FLAG_FULL = 1;
  const PANE_FRAME_FLAG_DEFLATE = 2;
  const PANE_FRAME_KNOWN_FLAGS = PANE_FRAME_FLAG_FULL | PANE_FRAME_FLAG_DEFLATE;
  const MAX_PANE_FRAME_BYTES = 2 * 1024 * 1024;
  const paneDeflateSupported = (() => {
    if (typeof DecompressionStream !== 'function') return false;
    try {
      new DecompressionStream('deflate');
      return true;
    } catch (_) {
      return false;
    }
  })();

  function xtermTheme(palette) {
    return {
      background: palette.panel_bg,
      foreground: palette.text,
      cursor: palette.accent,
      cursorAccent: palette.panel_bg,
      selectionBackground: palette.surface1,
      selectionForeground: palette.text,
      black: palette.surface_dim,
      red: palette.red,
      green: palette.green,
      yellow: palette.yellow,
      blue: palette.blue,
      magenta: palette.mauve,
      cyan: palette.teal,
      white: palette.text,
      brightBlack: palette.overlay0,
      brightRed: palette.red,
      brightGreen: palette.green,
      brightYellow: palette.yellow,
      brightBlue: palette.blue,
      brightMagenta: palette.mauve,
      brightCyan: palette.teal,
      brightWhite: palette.text,
    };
  }

  function applyHerdrTheme(theme) {
    const fingerprint = JSON.stringify(theme);
    if (fingerprint === appliedThemeFingerprint) return;
    const palette = theme.palette;
    const root = document.documentElement;
    const variables = {
      accent: palette.accent,
      background: palette.panel_bg,
      'surface-0': palette.surface0,
      'surface-1': palette.surface1,
      'surface-dim': palette.surface_dim,
      'overlay-0': palette.overlay0,
      'overlay-1': palette.overlay1,
      text: palette.text,
      subtext: palette.subtext0,
      red: palette.red,
      green: palette.green,
      yellow: palette.yellow,
    };
    for (const [name, value] of Object.entries(variables)) {
      root.style.setProperty(`--theme-${name}`, value);
    }
    root.style.colorScheme = theme.color_scheme;
    root.dataset.herdrTheme = theme.name;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = palette.panel_bg;
    terminalTheme = xtermTheme(palette);
    appliedThemeFingerprint = fingerprint;
    if (terminal) terminal.options.theme = terminalTheme;
    for (const pane of paneTerminals.values()) pane.terminal.options.theme = terminalTheme;
  }

  async function syncHerdrTheme() {
    if (themeSyncInFlight) return;
    themeSyncInFlight = true;
    const appearance = appearanceQuery.matches ? 'light' : 'dark';
    try {
      const response = await fetchWithTimeout(
        apiUrl(`theme?appearance=${appearance}`),
        { cache: 'no-store' },
        5_000,
      );
      if (!response.ok) throw new Error(`theme discovery failed (${response.status})`);
      applyHerdrTheme(await response.json());
    } catch (_) {
      // Keep the last valid palette when the service is temporarily unavailable.
    } finally {
      themeSyncInFlight = false;
    }
  }

  const terminalFontFamily = '"Herdr MesloLGS NF", monospace';
  await Promise.all([
    document.fonts.load('normal 400 14px "Herdr MesloLGS NF"'),
    document.fonts.load('normal 700 14px "Herdr MesloLGS NF"'),
    document.fonts.load('italic 400 14px "Herdr MesloLGS NF"'),
    document.fonts.load('italic 700 14px "Herdr MesloLGS NF"'),
    syncHerdrTheme(),
  ]);

  function syncPaneConnectionStatus() {
    const state = connectionIndicator.dataset.state || 'connecting';
    const detail = status.textContent || 'Connecting…';
    const label = state === 'connected'
      ? detail.includes('HTTP fallback') ? 'HTTP connected' : 'Connected'
      : state === 'disconnected' ? 'Disconnected' : detail;
    for (const pane of paneTerminals.values()) {
      const canReconnect = state === 'disconnected';
      pane.connection.dataset.state = state;
      pane.connection.textContent = label;
      pane.connection.disabled = !canReconnect;
      pane.connection.title = canReconnect ? `${detail}. Tap to reconnect.` : detail;
      pane.connection.setAttribute(
        'aria-label', canReconnect ? `${detail}. Reconnect terminal.` : detail,
      );
    }
  }

  function setStatus(message, state = 'connecting') {
    status.textContent = message;
    connectionIndicator.dataset.state = state;
    telemetry.dataset.state = state;
    telemetry.title = state === 'disconnected' ? 'Disconnected. Click to reconnect.' : '';
    syncPaneConnectionStatus();
    if (state === 'connected') {
      clearTimeout(reconnectReloadTimer);
      reconnectReloadTimer = undefined;
    }
  }

  function clearReconnectState() {
    clearTimeout(reconnectTimer);
    clearTimeout(reconnectStableTimer);
    clearTimeout(reconnectReloadTimer);
    reconnectTimer = undefined;
    reconnectStableTimer = undefined;
    reconnectReloadTimer = undefined;
  }

  function scheduleReconnectPageReload() {
    clearTimeout(reconnectReloadTimer);
    reconnectReloadTimer = setTimeout(() => {
      if (connectionIndicator.dataset.state === 'connected') return;
      if (!terminal || !currentBackend) return;
      location.reload();
    }, 7_000);
  }

  function attemptReconnect() {
    if (!terminal || !currentBackend || connectionIndicator.dataset.state !== 'disconnected') return;
    clearReconnectState();
    clearTimeout(connectTimer);
    connectTimer = undefined;
    socket?.close();
    socket = undefined;
    if (sessionId) {
      const oldSessionId = sessionId;
      sessionId = undefined;
      fetch(apiUrl(`sessions/${oldSessionId}`), { method: 'DELETE' }).catch(() => {});
    }
    httpFallbackStarting = false;
    reconnectAttempts = 0;
    setStatus('Reconnecting…');
    scheduleReconnectPageReload();
    if (viewMode === 'panes') startPaneWebSocket(currentBackend);
    else startWebSocket(currentBackend);
  }

  function requestReconnect() {
    if (!terminal || !currentBackend) return;
    setStatus('Reconnect requested', 'disconnected');
    attemptReconnect();
  }

  function startTransportRateMeter() {
    window.setInterval(() => {
      // Count terminal update messages, not browser canvas repaints.
      fps.textContent = `${receivedFrames} FPS`;
      receivedFrames = 0;
    }, 1000);
  }

  function showBrowserToast(message) {
    for (const current of toastHost.querySelectorAll('.web-toast:not(.clipboard-retry)')) {
      current.remove();
    }
    const toast = document.createElement('div');
    toast.className = 'web-toast';
    toast.textContent = message;
    toastHost.append(toast);
    window.setTimeout(() => toast.remove(), 5000);
  }

  async function writeBrowserClipboard(text) {
    if (!isSecureContext || !navigator.clipboard?.writeText) {
      throw new Error('Clipboard API is unavailable');
    }
    await navigator.clipboard.writeText(text);
  }

  function handleOsc52(payload) {
    // Herdr emits OSC 52;c;<base64> for a copied selection. This is the
    // client-local clipboard channel that normally targets a terminal host.
    const separator = payload.indexOf(';');
    if (separator < 0) return true;
    const encoded = payload.slice(separator + 1);
    if (!encoded || encoded === '?') return true;
    try {
      const binary = atob(encoded);
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      const text = new TextDecoder().decode(bytes);
      void writeBrowserClipboard(text).then(
        () => {},
        () => {
          const toast = document.createElement('button');
          toast.className = 'web-toast clipboard-retry';
          toast.type = 'button';
          toast.textContent = 'Clipboard permission needed — click to copy';
          toast.addEventListener('click', async () => {
            try {
              await writeBrowserClipboard(text);
              toast.remove();
            } catch (_) {
              toast.textContent = 'Clipboard access was denied by the browser';
            }
          });
          toastHost.querySelector('.clipboard-retry')?.remove();
          toastHost.append(toast);
        },
      );
    } catch (_) {
      showBrowserToast('Herdr sent invalid clipboard data');
    }
    return true;
  }

  function clipboardImageFromPaste(event) {
    const items = [...(event.clipboardData?.items || [])];
    const item = items.find((candidate) => clipboardImageExtensions[candidate.type]);
    if (!item) return undefined;
    const file = item.getAsFile();
    if (!file) return undefined;
    return { file, extension: clipboardImageExtensions[item.type] };
  }

  function appendInputOperation(operation) {
    if (inputOperations.length >= MAX_INPUT_OPERATIONS) {
      throw new RangeError('terminal input operation queue is full');
    }
    const retainedBytes = operation.retainedBytes || 0;
    const retainedOperationBytes = inputOperations.reduce(
      (total, queued) => total + (queued.retainedBytes || 0),
      0,
    );
    if (
      retainedBytes < 0
      || inputBuffer.length + retainedOperationBytes + retainedBytes
        > MAX_RETAINED_INPUT_OPERATION_BYTES
    ) {
      throw new RangeError('terminal input queue is full');
    }
    operation.offset = inputBuffer.enqueuedBytes;
    inputOperations.push(operation);
    scheduleInputDrain();
  }

  async function sendClipboardImage(image, pane) {
    if (viewMode === 'panes' && (!pane || !paneAcceptsInput(pane))) return;
    if (pane && !setActivePane(pane.streamId)) {
      showBrowserToast('Waiting for input to reach the previous pane');
      return;
    }
    if (socket?.readyState !== WebSocket.OPEN || !outputFlow?.attached) {
      showBrowserToast('Image paste needs a WebSocket connection');
      return;
    }
    if (image.file.size <= 0 || image.file.size > MAX_CLIPBOARD_IMAGE_BYTES) {
      showBrowserToast('Image must be smaller than 16 MiB');
      return;
    }

    // Preserve the last pointer position before the image operation.
    queuePendingMouseMotion();
    drainInput();

    // Record the byte position now. Input typed after this paste remains after
    // the image even if reading the browser File takes time.
    const operation = {
      kind: 'clipboard-image',
      extension: image.extension,
      ready: false,
      retainedBytes: image.file.size,
      streamId: pane?.streamId,
    };
    appendInputOperation(operation);
    try {
      operation.bytes = await image.file.arrayBuffer();
    } catch (error) {
      operation.error = error;
      throw error;
    } finally {
      operation.ready = true;
      scheduleInputDrain();
    }
  }

  function selectedBackendId() {
    return new URLSearchParams(location.search).get('session');
  }

  function setSelectedBackend(backendId) {
    const url = new URL(location.href);
    if (backendId) url.searchParams.set('session', backendId);
    else url.searchParams.delete('session');
    history.pushState({}, '', url);
  }

  function selectedViewMode() {
    if (mobileQuery.matches) return 'panes';
    return new URLSearchParams(location.search).get('view') === 'panes' ? 'panes' : 'full';
  }

  function setViewLocation(mode, state = {}) {
    const url = new URL(location.href);
    if (mode === 'panes') url.searchParams.set('view', 'panes');
    else url.searchParams.delete('view');
    for (const key of ['workspace', 'tab', 'pane']) {
      const value = state[key];
      if (mode === 'panes' && value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    history.replaceState({}, '', url);
  }

  function appBasePath() {
    // Jupyter Server Proxy canonicalizes /proxy/<port>/ to /proxy/<port>.
    // Build paths from the generated proxy prefix so the port is preserved.
    return window.herdrWebBasePath || (location.pathname.endsWith('/')
      ? location.pathname
      : `${location.pathname}/`);
  }

  function apiUrl(path) {
    return `${appBasePath()}api/${path}`;
  }

  function backendApiUrl(backend, path) {
    return apiUrl(`backends/${encodeURIComponent(backend.id)}/${path}`);
  }

  function navigationRecordLabel(record, fallback) {
    return record.label || record.terminal_title_stripped || record.agent || fallback;
  }

  function paneRecordLabel(record) {
    const explicit = record.label || record.terminal_title_stripped || record.agent;
    if (explicit) return explicit;
    const siblings = (navigationSnapshot?.panes || []).filter(
      (pane) => pane.tab_id === record.tab_id,
    );
    const index = siblings.findIndex((pane) => pane.pane_id === record.pane_id);
    return `Pane ${index >= 0 ? index + 1 : ''}`.trim();
  }

  function paneBreadcrumbLabel(record) {
    const maps = navigationMaps();
    const workspace = maps.workspaceById.get(record.workspace_id);
    const tab = maps.tabById.get(record.tab_id);
    return [
      navigationRecordLabel(workspace || {}, 'Space'),
      navigationRecordLabel(tab || {}, 'Tab'),
      paneRecordLabel(record),
    ].join(' · ');
  }

  function paneDisplayLabel(record) {
    return paneCompact ? paneBreadcrumbLabel(record) : paneRecordLabel(record);
  }

  function agentStatus(record) {
    const badge = document.createElement('span');
    badge.className = 'agent-status';
    badge.dataset.status = record.agent_status || 'unknown';
    badge.textContent = record.agent_status || 'unknown';
    return badge;
  }

  function navigationMaps() {
    const workspaces = navigationSnapshot?.workspaces || [];
    const tabs = navigationSnapshot?.tabs || [];
    const panes = navigationSnapshot?.panes || [];
    const agents = navigationSnapshot?.agents || [];
    return {
      workspaces,
      tabs,
      panes,
      agents,
      workspaceById: new Map(workspaces.map((record) => [record.workspace_id, record])),
      tabById: new Map(tabs.map((record) => [record.tab_id, record])),
      paneById: new Map(panes.map((record) => [record.pane_id, record])),
    };
  }

  function initializePaneSelection() {
    const maps = navigationMaps();
    const query = new URLSearchParams(location.search);
    const requestedPane = maps.paneById.get(query.get('pane'));
    const requestedTab = maps.tabById.get(query.get('tab'));
    const requestedWorkspace = maps.workspaceById.get(query.get('workspace'));
    selectedWorkspace = requestedPane?.workspace_id
      || requestedTab?.workspace_id
      || requestedWorkspace?.workspace_id
      || navigationSnapshot?.focused_workspace_id
      || maps.workspaces[0]?.workspace_id;
    const workspace = maps.workspaceById.get(selectedWorkspace);
    selectedTab = requestedPane?.tab_id
      || requestedTab?.tab_id
      || workspace?.active_tab_id
      || navigationSnapshot?.focused_tab_id
      || maps.tabs.find((record) => record.workspace_id === selectedWorkspace)?.tab_id;
    const layout = (navigationSnapshot?.layouts || []).find(
      (record) => record.tab_id === selectedTab,
    );
    selectedPane = requestedPane?.tab_id === selectedTab
      ? requestedPane.pane_id
      : layout?.focused_pane_id
        || navigationSnapshot?.focused_pane_id
        || maps.panes.find((record) => record.tab_id === selectedTab)?.pane_id;
    updatePaneLocation();
  }

  function updatePaneLocation() {
    if (viewMode !== 'panes') return;
    setViewLocation('panes', {
      workspace: selectedWorkspace,
      tab: selectedTab,
      pane: selectedPane,
    });
  }

  function desktopPaneNavigationItem(label, detail, current, statusValue, action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pane-sidebar-item';
    if (current) button.setAttribute('aria-current', 'true');
    const name = document.createElement('span');
    name.className = 'pane-sidebar-item-label';
    name.textContent = label;
    button.append(name);
    if (detail) {
      const description = document.createElement('span');
      description.className = 'pane-sidebar-item-detail';
      description.textContent = detail;
      button.append(description);
    }
    if (statusValue && statusValue !== 'unknown') {
      button.dataset.status = statusValue;
      const status = document.createElement('span');
      status.className = 'visually-hidden';
      status.textContent = `Status: ${statusValue}`;
      button.append(status);
    }
    button.addEventListener('click', action);
    return button;
  }

  function readDesktopPaneSidebarCollapsed() {
    try {
      return localStorage.getItem(DESKTOP_PANE_SIDEBAR_COLLAPSED_KEY) === 'true';
    } catch (_) {
      return false;
    }
  }

  function saveDesktopPaneSidebarCollapsed() {
    try {
      if (desktopPaneSidebarCollapsed) {
        localStorage.setItem(DESKTOP_PANE_SIDEBAR_COLLAPSED_KEY, 'true');
      } else {
        localStorage.removeItem(DESKTOP_PANE_SIDEBAR_COLLAPSED_KEY);
      }
    } catch (_) {
      // The sidebar state still applies for this page when storage is unavailable.
    }
  }

  function renderDesktopPaneSidebar() {
    panesView.dataset.sidebarCollapsed = String(desktopPaneSidebarCollapsed);
    paneSidebarToggle.setAttribute('aria-expanded', String(!desktopPaneSidebarCollapsed));
    paneSidebarToggle.setAttribute(
      'aria-label', desktopPaneSidebarCollapsed ? 'Show sidebar' : 'Hide sidebar',
    );
    paneSidebarToggle.title = desktopPaneSidebarCollapsed ? 'Show sidebar' : 'Hide sidebar';
  }

  function setDesktopPaneSidebarCollapsed(collapsed) {
    if (desktopPaneSidebarCollapsed === collapsed) return;
    desktopPaneSidebarCollapsed = collapsed;
    saveDesktopPaneSidebarCollapsed();
    renderDesktopPaneSidebar();
    sendPaneResizes();
    if (!mobileQuery.matches) selectedPaneTerminal()?.terminal.focus();
  }

  function renderPaneNavigation() {
    if (viewMode !== 'panes' || !navigationSnapshot) return;
    const maps = navigationMaps();
    const workspaceLabels = new Map(
      maps.workspaces.map((record) => [
        record.workspace_id, navigationRecordLabel(record, 'Space'),
      ]),
    );
    const tabLabels = new Map(
      maps.tabs.map((record) => [record.tab_id, navigationRecordLabel(record, 'Tab')]),
    );

    paneWorkspaces.replaceChildren();
    for (const workspace of maps.workspaces) {
      const tabCount = workspace.tab_count
        ?? maps.tabs.filter((record) => record.workspace_id === workspace.workspace_id).length;
      const paneCount = workspace.pane_count
        ?? maps.panes.filter((record) => record.workspace_id === workspace.workspace_id).length;
      const detail = `${tabCount} ${tabCount === 1 ? 'tab' : 'tabs'} · ${paneCount} ${paneCount === 1 ? 'pane' : 'panes'}`;
      paneWorkspaces.append(desktopPaneNavigationItem(
        navigationRecordLabel(workspace, 'Space'),
        detail,
        workspace.workspace_id === selectedWorkspace,
        workspace.agent_status,
        () => selectPaneWorkspace(workspace.workspace_id),
      ));
    }
    if (!maps.workspaces.length) {
      const empty = document.createElement('p');
      empty.className = 'pane-sidebar-empty';
      empty.textContent = 'No spaces';
      paneWorkspaces.append(empty);
    }

    paneAgents.replaceChildren();
    for (const agent of maps.agents) {
      const detail = [
        workspaceLabels.get(agent.workspace_id),
        tabLabels.get(agent.tab_id),
      ].filter(Boolean).join(' · ');
      paneAgents.append(desktopPaneNavigationItem(
        navigationRecordLabel(agent, 'Agent'),
        detail,
        agent.pane_id === selectedPane,
        agent.agent_status,
        () => selectPaneTarget(agent.pane_id),
      ));
    }
    if (!maps.agents.length) {
      const empty = document.createElement('p');
      empty.className = 'pane-sidebar-empty';
      empty.textContent = 'No agents';
      paneAgents.append(empty);
    }

    paneTabs.replaceChildren();
    for (const tab of maps.tabs.filter((record) => record.workspace_id === selectedWorkspace)) {
      const button = document.createElement('button');
      button.type = 'button';
      button.role = 'tab';
      button.textContent = navigationRecordLabel(tab, 'Tab');
      button.setAttribute('aria-selected', String(tab.tab_id === selectedTab));
      button.addEventListener('click', () => selectPaneTab(tab.tab_id));
      paneTabs.append(button);
    }
    refreshPaneTitles();
  }

  function updatePaneTitle(pane) {
    const record = navigationMaps().paneById.get(pane.paneId);
    if (record) {
      const label = paneDisplayLabel(record);
      pane.label.textContent = label;
      pane.tile.setAttribute('aria-label', label);
    }
    const agentStatusValue = record?.agent_status;
    pane.state.textContent = pane.closed
      ? 'Closed'
      : pane.mode === 'observe'
        ? 'Read-only'
        : agentStatusValue && agentStatusValue !== 'unknown'
          ? agentStatusValue
          : '';
    syncPaneKeyboardHelper(pane);
  }

  function refreshPaneTitles() {
    for (const pane of paneTerminals.values()) updatePaneTitle(pane);
  }

  function clampMobileTerminalFontSize(value) {
    const size = Math.round(Number(value));
    if (!Number.isFinite(size)) return DEFAULT_TERMINAL_FONT_SIZE;
    return Math.max(MIN_MOBILE_TERMINAL_FONT_SIZE, Math.min(MAX_MOBILE_TERMINAL_FONT_SIZE, size));
  }

  function readMobileTerminalFontSize() {
    try {
      const stored = localStorage.getItem(MOBILE_TERMINAL_FONT_SIZE_KEY);
      return stored === null ? DEFAULT_TERMINAL_FONT_SIZE : clampMobileTerminalFontSize(stored);
    } catch (_) {
      return DEFAULT_TERMINAL_FONT_SIZE;
    }
  }

  function saveMobileTerminalFontSize() {
    try {
      if (mobileTerminalFontSize === DEFAULT_TERMINAL_FONT_SIZE) {
        localStorage.removeItem(MOBILE_TERMINAL_FONT_SIZE_KEY);
      } else {
        localStorage.setItem(MOBILE_TERMINAL_FONT_SIZE_KEY, String(mobileTerminalFontSize));
      }
    } catch (_) {
      // Terminal size still applies for this page when storage is unavailable.
    }
  }

  function mobileTerminalZoomLabel() {
    return `${Math.round((mobileTerminalFontSize / DEFAULT_TERMINAL_FONT_SIZE) * 100)}%`;
  }

  function setMobileTerminalFontSize(value) {
    const nextSize = clampMobileTerminalFontSize(value);
    if (nextSize === mobileTerminalFontSize) return;
    mobileTerminalFontSize = nextSize;
    saveMobileTerminalFontSize();
    for (const pane of paneTerminals.values()) pane.terminal.options.fontSize = nextSize;
    sendPaneResizes();
  }

  function terminalZoomControls() {
    const controls = document.createElement('div');
    controls.className = 'terminal-zoom';
    controls.setAttribute('role', 'group');
    controls.setAttribute('aria-label', 'Terminal size');
    const smaller = document.createElement('button');
    smaller.type = 'button';
    smaller.textContent = 'A−';
    smaller.setAttribute('aria-label', 'Make terminal text smaller');
    const value = document.createElement('output');
    value.setAttribute('aria-live', 'polite');
    const larger = document.createElement('button');
    larger.type = 'button';
    larger.textContent = 'A+';
    larger.setAttribute('aria-label', 'Make terminal text larger');
    const reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'terminal-zoom-reset';
    reset.textContent = 'Reset';
    const sync = () => {
      value.textContent = `${mobileTerminalZoomLabel()} · ${mobileTerminalFontSize} px`;
      smaller.disabled = mobileTerminalFontSize <= MIN_MOBILE_TERMINAL_FONT_SIZE;
      larger.disabled = mobileTerminalFontSize >= MAX_MOBILE_TERMINAL_FONT_SIZE;
      reset.disabled = mobileTerminalFontSize === DEFAULT_TERMINAL_FONT_SIZE;
    };
    smaller.addEventListener('click', () => {
      setMobileTerminalFontSize(mobileTerminalFontSize - 1);
      sync();
    });
    larger.addEventListener('click', () => {
      setMobileTerminalFontSize(mobileTerminalFontSize + 1);
      sync();
    });
    reset.addEventListener('click', () => {
      setMobileTerminalFontSize(DEFAULT_TERMINAL_FONT_SIZE);
      sync();
    });
    controls.append(smaller, value, larger, reset);
    sync();
    return controls;
  }

  function sheetHeading(text) {
    const heading = document.createElement('h3');
    heading.className = 'sheet-heading';
    heading.textContent = text;
    return heading;
  }

  function preparePaneBrowseExpansion() {
    const backendId = currentBackend?.id;
    if (paneBrowseExpansionBackendId !== backendId) {
      paneBrowseExpansionBackendId = backendId;
      paneBrowseExpandedWorkspaces.clear();
      paneBrowseExpandedTabs.clear();
    }
    if (selectedWorkspace) paneBrowseExpandedWorkspaces.add(selectedWorkspace);
    if (selectedTab) paneBrowseExpandedTabs.add(selectedTab);
  }

  function togglePaneBrowseBranch(kind, targetId) {
    const expanded = kind === 'workspace'
      ? paneBrowseExpandedWorkspaces : paneBrowseExpandedTabs;
    if (expanded.has(targetId)) expanded.delete(targetId);
    else expanded.add(targetId);
    const previousScrollTop = sheetContent.scrollTop;
    renderMobileSheet();
    sheetContent.scrollTop = previousScrollTop;
    const target = [...sheetContent.querySelectorAll(`[data-tree-kind="${kind}"]`)].find(
      (button) => button.dataset.treeId === targetId,
    );
    target?.focus({ preventScroll: true });
  }

  function paneBrowseChevronIcon() {
    const namespace = 'http://www.w3.org/2000/svg';
    const icon = document.createElementNS(namespace, 'svg');
    icon.classList.add('sheet-tree-chevron');
    icon.setAttribute('viewBox', '6 4 12 16');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    const path = document.createElementNS(namespace, 'path');
    path.setAttribute('d', 'm9 18 6-6-6-6');
    icon.append(path);
    return icon;
  }

  function configurePaneBrowseBranch(button, kind, targetId, expanded) {
    button.classList.add('sheet-tree-branch');
    button.dataset.treeKind = kind;
    button.dataset.treeId = targetId;
    button.setAttribute('aria-expanded', String(expanded));
    button.querySelector('.sheet-item-label')?.prepend(paneBrowseChevronIcon());
  }

  function renderPaneBrowse() {
    if (!navigationSnapshot) {
      const loading = document.createElement('p');
      loading.className = 'sheet-empty';
      loading.textContent = 'Loading…';
      sheetContent.append(loading);
      return;
    }
    const maps = navigationMaps();
    const workspaceIds = new Set(maps.workspaces.map((record) => record.workspace_id));
    const tabIds = new Set(maps.tabs.map((record) => record.tab_id));
    for (const id of paneBrowseExpandedWorkspaces) {
      if (!workspaceIds.has(id)) paneBrowseExpandedWorkspaces.delete(id);
    }
    for (const id of paneBrowseExpandedTabs) {
      if (!tabIds.has(id)) paneBrowseExpandedTabs.delete(id);
    }

    const tree = document.createElement('ul');
    tree.className = 'sheet-tree';
    tree.setAttribute('aria-label', 'Spaces, tabs, and panes');

    for (const workspace of maps.workspaces) {
      const workspaceTabs = maps.tabs.filter(
        (record) => record.workspace_id === workspace.workspace_id,
      );
      const workspaceNode = document.createElement('li');
      const workspaceExpanded = paneBrowseExpandedWorkspaces.has(workspace.workspace_id);
      const workspaceButton = sheetItem(
        navigationRecordLabel(workspace, 'Space'),
        `${workspaceTabs.length} ${workspaceTabs.length === 1 ? 'tab' : 'tabs'}`,
        workspace.workspace_id === selectedWorkspace,
        workspace.agent_status,
        workspaceTabs.length
          ? () => togglePaneBrowseBranch('workspace', workspace.workspace_id)
          : () => selectPaneWorkspace(workspace.workspace_id),
      );
      workspaceButton.classList.add('sheet-tree-item');
      if (workspaceTabs.length) {
        configurePaneBrowseBranch(
          workspaceButton, 'workspace', workspace.workspace_id, workspaceExpanded,
        );
      }
      workspaceNode.append(workspaceButton);

      if (workspaceTabs.length && workspaceExpanded) {
        const tabList = document.createElement('ul');
        for (const tab of workspaceTabs) {
          const tabPanes = maps.panes.filter((record) => record.tab_id === tab.tab_id);
          const tabNode = document.createElement('li');
          const tabExpanded = paneBrowseExpandedTabs.has(tab.tab_id);
          const tabButton = sheetItem(
            navigationRecordLabel(tab, 'Tab'),
            undefined,
            tab.tab_id === selectedTab,
            tab.agent_status,
            tabPanes.length > 1
              ? () => togglePaneBrowseBranch('tab', tab.tab_id)
              : () => selectPaneTab(tab.tab_id),
          );
          tabButton.classList.add('sheet-tree-item');
          if (tabPanes.length > 1) {
            configurePaneBrowseBranch(tabButton, 'tab', tab.tab_id, tabExpanded);
          }
          tabNode.append(tabButton);

          if (tabPanes.length > 1 && tabExpanded) {
            const paneList = document.createElement('ul');
            for (const pane of tabPanes) {
              const paneNode = document.createElement('li');
              const paneButton = sheetItem(
                paneRecordLabel(pane),
                undefined,
                pane.pane_id === selectedPane,
                pane.agent_status,
                () => selectPaneTarget(pane.pane_id),
              );
              paneButton.classList.add('sheet-tree-item');
              paneNode.append(paneButton);
              paneList.append(paneNode);
            }
            tabNode.append(paneList);
          }
          tabList.append(tabNode);
        }
        workspaceNode.append(tabList);
      }
      tree.append(workspaceNode);
    }

    if (maps.workspaces.length) sheetContent.append(tree);
    else {
      const empty = document.createElement('p');
      empty.className = 'sheet-empty';
      empty.textContent = 'No spaces found.';
      sheetContent.append(empty);
    }

    const options = document.createElement('div');
    options.className = 'sheet-options';
    options.append(sheetHeading('Terminal size'), terminalZoomControls());
    sheetContent.append(options);
  }

  function sheetItem(label, detail, current, statusValue, action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'sheet-item';
    if (current) button.setAttribute('aria-current', 'true');
    const name = document.createElement('span');
    name.className = 'sheet-item-label';
    const nameText = document.createElement('span');
    nameText.className = 'sheet-item-label-text';
    nameText.textContent = label;
    name.append(nameText);
    button.append(name);
    if (detail) {
      button.classList.add('sheet-item-has-detail');
      const description = document.createElement('span');
      description.className = 'sheet-item-detail';
      description.textContent = detail;
      button.append(description);
    }
    if (statusValue && statusValue !== 'unknown') {
      button.dataset.status = statusValue;
      button.append(agentStatus({ agent_status: statusValue }));
    }
    button.addEventListener('click', action);
    return button;
  }

  function closeMobileSheet(refocus = true) {
    openSheetName = undefined;
    cancelAnimationFrame(mobileSheetOpenFrame);
    mobileSheetOpenFrame = undefined;
    clearTimeout(mobileSheetCloseTimer);
    mobileSheetCloseTimer = undefined;
    for (const button of [...mobileToolbar.querySelectorAll('button'), paneBrowse]) {
      button.setAttribute('aria-expanded', 'false');
    }
    const finish = () => {
      if (mobileSheet.dataset.state !== 'closing') return;
      mobileSheet.hidden = true;
      delete mobileSheet.dataset.state;
      mobileSheetCloseTimer = undefined;
      if (refocus && !mobileQuery.matches) terminal?.focus();
    };
    if (mobileSheet.hidden) {
      if (refocus && !mobileQuery.matches) terminal?.focus();
      return;
    }
    mobileSheet.dataset.state = 'closing';
    mobileSheet.setAttribute('aria-hidden', 'true');
    if (reducedMotionQuery.matches) finish();
    else mobileSheetCloseTimer = setTimeout(finish, MOBILE_SHEET_ANIMATION_MS);
  }

  async function focusNavigationTarget(kind, targetId, button) {
    if (!currentBackend) return;
    button.disabled = true;
    try {
      const response = await fetchWithTimeout(
        backendApiUrl(currentBackend, 'focus'),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind, target_id: targetId }),
        },
      );
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `could not focus ${kind}`);
      navigationSnapshot = result;
      navigationFingerprint = JSON.stringify(result);
      closeMobileSheet();
    } catch (error) {
      button.disabled = false;
      showBrowserToast(error.message);
    }
  }

  function renderMobileSheet() {
    if (!openSheetName) return;
    sheetContent.replaceChildren();
    sheetTitle.textContent = {
      spaces: 'Spaces', tabs: 'Tabs', agents: 'Agents', more: 'More', browse: 'Browse',
    }[openSheetName];

    if (openSheetName === 'browse') {
      renderPaneBrowse();
      return;
    }

    if (openSheetName === 'more') {
      const panesButton = document.createElement('button');
      panesButton.type = 'button';
      panesButton.className = 'more-action';
      panesButton.textContent = 'Open Panes mode';
      panesButton.addEventListener('click', () => switchViewMode('panes'));
      sheetContent.append(panesButton);
      return;
    }

    if (!navigationSnapshot) {
      const loading = document.createElement('p');
      loading.className = 'sheet-empty';
      loading.textContent = 'Loading…';
      sheetContent.append(loading);
      return;
    }

    const workspaces = navigationSnapshot.workspaces || [];
    const tabs = navigationSnapshot.tabs || [];
    let records;
    let kind;
    if (openSheetName === 'spaces') {
      records = workspaces;
      kind = 'workspace';
    } else if (openSheetName === 'tabs') {
      records = tabs.filter(
        (record) => record.workspace_id === navigationSnapshot.focused_workspace_id,
      );
      kind = 'tab';
    } else {
      records = navigationSnapshot.agents || [];
      kind = 'agent';
    }

    const workspaceLabels = new Map(
      workspaces.map((record) => [record.workspace_id, navigationRecordLabel(record, 'Space')]),
    );
    const tabLabels = new Map(
      tabs.map((record) => [record.tab_id, navigationRecordLabel(record, 'Tab')]),
    );
    for (const record of records) {
      const targetId = kind === 'workspace'
        ? record.workspace_id : kind === 'tab' ? record.tab_id : record.pane_id;
      const fallback = kind === 'workspace' ? 'Space' : kind === 'tab' ? 'Tab' : 'Agent';
      let detail;
      if (kind === 'workspace') {
        const tabCount = record.tab_count || 0;
        const paneCount = record.pane_count || 0;
        detail = `${tabCount} ${tabCount === 1 ? 'tab' : 'tabs'} · ${paneCount} ${paneCount === 1 ? 'pane' : 'panes'}`;
      } else if (kind === 'tab') {
        const paneCount = record.pane_count || 0;
        detail = `${paneCount} ${paneCount === 1 ? 'pane' : 'panes'}`;
      } else {
        detail = `${workspaceLabels.get(record.workspace_id) || 'Space'} · ${tabLabels.get(record.tab_id) || 'Tab'}`;
      }
      const current = targetId === navigationSnapshot[`focused_${kind === 'agent' ? 'pane' : kind}_id`];
      const button = sheetItem(
        navigationRecordLabel(record, fallback), detail, current, record.agent_status,
        () => focusNavigationTarget(kind, targetId, button),
      );
      sheetContent.append(button);
    }
    if (!records.length) {
      const empty = document.createElement('p');
      empty.className = 'sheet-empty';
      empty.textContent = `No ${openSheetName.toLowerCase()} found.`;
      sheetContent.append(empty);
    }
  }

  async function refreshNavigation() {
    if (!currentBackend || (viewMode !== 'panes' && !mobileQuery.matches) || document.hidden) return;
    if (navigationRequest) return navigationRequest;
    const backend = currentBackend;
    navigationRequest = fetchWithTimeout(
      backendApiUrl(backend, 'navigation'), { cache: 'no-store' }, 5_000,
    ).then(async (response) => {
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'navigation state is unavailable');
      if (currentBackend?.id !== backend.id) return;
      const fingerprint = JSON.stringify(result);
      if (fingerprint === navigationFingerprint) return;
      const previousStructure = viewMode === 'panes' && paneTerminals.size
        ? paneStructureFingerprint(navigationSnapshot, selectedTab)
        : undefined;
      navigationSnapshot = result;
      navigationFingerprint = fingerprint;
      if (viewMode === 'panes') {
        const structureChanged = previousStructure !== undefined
          && previousStructure !== paneStructureFingerprint(result, selectedTab);
        renderPaneNavigation();
        const selectedPaneExists = (result.panes || []).some(
          (pane) => pane.pane_id === selectedPane && pane.tab_id === selectedTab,
        );
        if (structureChanged && (!paneCompact || !selectedPaneExists)) {
          schedulePaneStructureRebuild();
        }
      }
      renderMobileSheet();
    }).catch((error) => {
      if (openSheetName) showBrowserToast(error.message);
    }).finally(() => {
      navigationRequest = undefined;
    });
    return navigationRequest;
  }

  function startNavigationPolling() {
    clearInterval(navigationTimer);
    navigationTimer = undefined;
    navigationSnapshot = undefined;
    navigationFingerprint = undefined;
    if ((viewMode !== 'panes' && !mobileQuery.matches) || !currentBackend) return;
    void refreshNavigation();
    navigationTimer = setInterval(refreshNavigation, 2_000);
  }

  function openMobileSheet(name, trigger) {
    const previousSheetName = openSheetName;
    openSheetName = name;
    if (name === 'browse' && previousSheetName !== 'browse') preparePaneBrowseExpansion();
    clearTimeout(mobileSheetCloseTimer);
    mobileSheetCloseTimer = undefined;
    cancelAnimationFrame(mobileSheetOpenFrame);
    mobileSheetOpenFrame = undefined;
    const animate = mobileSheet.hidden || mobileSheet.dataset.state === 'closing';
    mobileSheet.hidden = false;
    mobileSheet.removeAttribute('aria-hidden');
    mobileSheet.dataset.state = animate ? 'opening' : 'open';
    for (const button of [...mobileToolbar.querySelectorAll('button'), paneBrowse]) {
      button.setAttribute('aria-expanded', button === trigger ? 'true' : 'false');
    }
    renderMobileSheet();
    if (animate && !reducedMotionQuery.matches) {
      mobileSheetPanel.getBoundingClientRect();
      mobileSheetOpenFrame = requestAnimationFrame(() => {
        mobileSheetOpenFrame = undefined;
        if (openSheetName) mobileSheet.dataset.state = 'open';
      });
    } else {
      mobileSheet.dataset.state = 'open';
    }
    if (name !== 'more') void refreshNavigation();
    document.querySelector('#sheet-close').focus();
  }

  function requiresAuthentication(response) {
    const contentType = response.headers.get('Content-Type') || '';
    return (response.status === 401 || response.status === 403)
      && contentType.toLowerCase().includes('text/html');
  }

  function beginAuthenticationReload() {
    if (authenticationReloading) return;
    authenticationReloading = true;
    clearReconnectState();
    clearTimeout(connectTimer);
    connectTimer = undefined;
    setStatus('Authentication expired', 'disconnected');
    setTimeout(() => location.reload(), 50);
  }

  function reloadForAuthentication(response) {
    if (!requiresAuthentication(response)) return false;
    beginAuthenticationReload();
    return true;
  }

  async function fetchWithTimeout(url, options = {}, timeout = 10_000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      if (reloadForAuthentication(response)) throw new Error('Authentication expired');
      return response;
    } finally {
      clearTimeout(timer);
    }
  }

  function wsUrl(backendId) {
    const url = new URL(
      `${appBasePath()}ws/${encodeURIComponent(backendId)}`, location.href,
    );
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.toString();
  }

  function clearOutputFlow(flow = outputFlow) {
    if (!flow) return;
    clearTimeout(flow.ackTimer);
    flow.ackTimer = undefined;
    if (outputFlow === flow) outputFlow = undefined;
  }

  function sendOutputAcknowledgement(flow) {
    clearTimeout(flow.ackTimer);
    flow.ackTimer = undefined;
    if (
      flow !== outputFlow || flow.socket.readyState !== WebSocket.OPEN
      || flow.parsedBytes <= flow.acknowledgedBytes
    ) return;
    flow.socket.send(JSON.stringify({ type: 'output-ack', bytes: flow.parsedBytes }));
    flow.acknowledgedBytes = flow.parsedBytes;
  }

  function noteParsedOutput(flow, length) {
    if (flow !== outputFlow) return;
    flow.parsedBytes += length;
    if (flow.parsedBytes - flow.acknowledgedBytes >= OUTPUT_ACK_BATCH_BYTES) {
      sendOutputAcknowledgement(flow);
    } else if (flow.ackTimer === undefined) {
      flow.ackTimer = setTimeout(
        () => sendOutputAcknowledgement(flow), OUTPUT_ACK_DELAY_MS,
      );
    }
  }

  function enableWebglRenderer(activeTerminal) {
    let addon;
    try {
      addon = new WebglAddon();
      addon.onContextLoss(() => {
        if (webglAddon !== addon) return;
        webglAddon = undefined;
        addon.dispose();
      });
      activeTerminal.loadAddon(addon);
      webglAddon = addon;
    } catch (_) {
      // xterm keeps its DOM renderer when WebGL2 is unavailable.
      addon?.dispose();
      webglAddon = undefined;
    }
  }

  function paneStructureFingerprint(snapshot, tabId) {
    const layout = (snapshot?.layouts || []).find((candidate) => candidate.tab_id === tabId);
    const availablePaneIds = (snapshot?.panes || [])
      .filter((pane) => pane.tab_id === tabId)
      .map((pane) => pane.pane_id)
      .sort();
    const area = layout?.area;
    const geometry = (layout?.panes || []).map((pane) => {
      const rect = pane.rect;
      if (!area || area.width <= 0 || area.height <= 0) return { pane_id: pane.pane_id };
      return {
        pane_id: pane.pane_id,
        x: Number(((rect.x - area.x) / area.width).toFixed(3)),
        y: Number(((rect.y - area.y) / area.height).toFixed(3)),
        width: Number((rect.width / area.width).toFixed(3)),
        height: Number((rect.height / area.height).toFixed(3)),
      };
    }).sort((left, right) => left.pane_id.localeCompare(right.pane_id));
    return JSON.stringify({ availablePaneIds, geometry });
  }

  function paneLayoutForTab(tabId) {
    return (navigationSnapshot?.layouts || []).find((layout) => layout.tab_id === tabId);
  }

  function disposePaneTerminals() {
    stopMobileKeyRepeat();
    resetMobileModifiers();
    for (const pane of paneTerminals.values()) {
      pane.cancelTouchScroll?.();
      clearTimeout(pane.scrollFlushTimer);
      resetMobilePaneInput(pane);
      closeTerminalSnapshot(pane, false);
      pane.terminal.dispose();
    }
    paneTerminals.clear();
    syncTerminalSnapshotControls();
    currentPaneRequests = [];
    pendingPaneActivation = undefined;
    paneGrid.replaceChildren();
    if (viewMode === 'panes') {
      terminal = undefined;
      fitAddon = undefined;
    }
  }

  function setActivePane(streamId, notifyServer = true) {
    const pane = paneTerminals.get(streamId);
    if (!pane) return false;
    if (terminal === pane.terminal && selectedPane === pane.paneId) return true;
    queuePendingMouseMotion();
    drainInput();
    if (inputBuffer.length || inputOperations.length) {
      pendingPaneActivation = streamId;
      scheduleInputDrain(8);
      return false;
    }
    pendingPaneActivation = undefined;
    selectedPane = pane.paneId;
    terminal = pane.terminal;
    fitAddon = pane.fitAddon;
    if (outputFlow?.paneMode) {
      outputFlow.inputReady = pane.mode === 'control' && !pane.closed && !pane.awaitingFull;
    }
    for (const candidate of paneTerminals.values()) {
      candidate.tile.dataset.active = String(candidate.streamId === streamId);
    }
    if (notifyServer && socket?.readyState === WebSocket.OPEN && outputFlow?.paneMode) {
      socket.send(JSON.stringify({ type: 'pane-active', stream_id: streamId }));
    }
    updatePaneLocation();
    renderPaneNavigation();
    syncPaneKeyboardHelper(pane);
    return true;
  }

  function sendPaneScrollNow(pane, direction, lines, column, row) {
    if (socket?.readyState !== WebSocket.OPEN || !outputFlow?.paneMode) return false;
    if (!paneAcceptsInput(pane)) return false;
    const message = {
      type: 'pane-scroll',
      stream_id: pane.streamId,
      direction,
      lines,
    };
    if (Number.isInteger(column)) message.column = column;
    if (Number.isInteger(row)) message.row = row;
    socket.send(JSON.stringify(message));
    pane.lastScrollSentAt = performance.now();
    pane.scrollAwaitingFrame = true;
    return true;
  }

  function flushPaneScroll(pane, force = false) {
    clearTimeout(pane.scrollFlushTimer);
    pane.scrollFlushTimer = undefined;
    const delta = pane.pendingScrollDelta || 0;
    if (!delta) return;
    const elapsed = performance.now() - (pane.lastScrollSentAt || 0);
    if (pane.scrollAwaitingFrame && !force) {
      pane.scrollFlushTimer = setTimeout(
        () => flushPaneScroll(pane, true),
        Math.max(0, PANE_SCROLL_FRAME_TIMEOUT_MS - elapsed),
      );
      return;
    }
    if (elapsed < PANE_SCROLL_FLUSH_MS) {
      pane.scrollFlushTimer = setTimeout(
        () => flushPaneScroll(pane, force),
        PANE_SCROLL_FLUSH_MS - elapsed,
      );
      return;
    }
    pane.pendingScrollDelta = 0;
    sendPaneScrollNow(
      pane,
      delta < 0 ? 'up' : 'down',
      Math.abs(delta),
      pane.pendingScrollColumn,
      pane.pendingScrollRow,
    );
  }

  function sendPaneScroll(pane, direction, lines, column, row) {
    if (socket?.readyState !== WebSocket.OPEN || !outputFlow?.paneMode) return false;
    if (!paneAcceptsInput(pane)) return false;
    pane.pendingScrollDelta = (pane.pendingScrollDelta || 0)
      + (direction === 'up' ? -lines : lines);
    pane.pendingScrollColumn = column;
    pane.pendingScrollRow = row;
    if (pane.scrollFlushTimer === undefined) flushPaneScroll(pane);
    return true;
  }

  function renderMobileModifierState() {
    for (const button of mobileModifiers.querySelectorAll('[data-modifier]')) {
      button.setAttribute(
        'aria-pressed',
        String(mobileModifierState[button.dataset.modifier] === true),
      );
    }
  }

  function resetMobileModifiers() {
    for (const name of Object.keys(mobileModifierState)) mobileModifierState[name] = false;
    renderMobileModifierState();
  }

  function renderMobileNavigationMode() {
    mobileNavigationModeButton.setAttribute('aria-pressed', String(mobileNavigationMode));
    mobileNavigationModeButton.setAttribute(
      'aria-label', mobileNavigationMode ? 'Show primary keys' : 'Show navigation keys',
    );
    mobileDefaultRow.hidden = mobileNavigationMode;
    mobileNavigationRow.hidden = !mobileNavigationMode;
  }

  function setMobileNavigationMode(enabled) {
    stopMobileKeyRepeat();
    mobileNavigationMode = enabled;
    renderMobileNavigationMode();
    focusTerminalAfterControl();
  }

  function focusTerminalAfterControl() {
    if (!mobileQuery.matches) {
      terminal?.focus();
      return;
    }
    const pane = selectedPaneTerminal();
    if (!pane) return;
    const keyboardFocused = document.activeElement === paneKeyboardHelper(pane);
    if (!keyboardFocused && document.documentElement.dataset.mobileKeyboard !== 'open') return;
    focusPaneKeyboard(pane);
  }

  function toggleMobileModifier(name) {
    if (!(name in mobileModifierState)) return;
    mobileModifierState[name] = !mobileModifierState[name];
    renderMobileModifierState();
    focusTerminalAfterControl();
  }

  function applyMobileModifiers(data) {
    if (!mobileQuery.matches || !Object.values(mobileModifierState).some(Boolean)) return data;
    if (data === '\r') return terminalDataForModifiedEnter(mobileModifierState) || data;
    const characters = [...data];
    if (characters.length !== 1) return data;
    let character = characters[0];
    if (mobileModifierState.shift) {
      const shifted = {
        '1': '!', '2': '@', '3': '#', '4': '$', '5': '%',
        '6': '^', '7': '&', '8': '*', '9': '(', '0': ')',
        '-': '_', '=': '+', '[': '{', ']': '}', '\\': '|',
        ';': ':', "'": '"', ',': '<', '.': '>', '/': '?', '`': '~',
      };
      character = shifted[character] || character.toUpperCase();
    }
    if (mobileModifierState.control) {
      const code = character.toUpperCase().charCodeAt(0);
      if (code >= 64 && code <= 95) character = String.fromCharCode(code & 31);
      else if (character === ' ') character = '\x00';
      else if (character === '?') character = '\x7f';
    }
    return mobileModifierState.alt ? `\x1b${character}` : character;
  }

  function selectedPaneTerminal() {
    return [...paneTerminals.values()].find(
      (candidate) => candidate.paneId === selectedPane,
    );
  }

  function paneAcceptsInput(pane, announce = true) {
    if (!pane) return false;
    if (pane.closed) {
      if (announce) showBrowserToast('This pane is closed');
      return false;
    }
    if (pane.mode !== 'control') {
      if (announce) showBrowserToast('This pane is read-only');
      return false;
    }
    return true;
  }

  function activateWritablePane() {
    const pane = selectedPaneTerminal();
    if (pane?.snapshot) return undefined;
    if (!paneAcceptsInput(pane)) return undefined;
    return setActivePane(pane.streamId) ? pane : undefined;
  }

  function sendMobileTerminalKey(key) {
    const pane = activateWritablePane();
    if (!pane) return;
    let data;
    if (key === 'escape') {
      data = mobileModifierState.alt ? '\x1b\x1b' : '\x1b';
    } else if (key === 'tab') {
      data = mobileModifierState.shift ? '\x1b[Z' : '\t';
      if (mobileModifierState.alt) data = `\x1b${data}`;
    } else {
      return;
    }
    clearMobilePredictionState(pane, true);
    sendInput(data);
    focusTerminalAfterControl();
  }

  function sendMobileNavigationKey(key) {
    const data = terminalDataForNavigationKey(key, mobileModifierState);
    const pane = activateWritablePane();
    if (!data || !pane) return;
    clearMobilePredictionState(pane, true);
    sendInput(data);
    focusTerminalAfterControl();
  }

  function appendTerminalSnapshotColor(codes, cell, foreground) {
    const isDefault = foreground ? cell.isFgDefault() : cell.isBgDefault();
    if (isDefault) return;
    const color = foreground ? cell.getFgColor() : cell.getBgColor();
    const rgb = foreground ? cell.isFgRGB() : cell.isBgRGB();
    const palette = foreground ? cell.isFgPalette() : cell.isBgPalette();
    const base = foreground ? 38 : 48;
    if (rgb) {
      codes.push(base, 2, (color >> 16) & 0xff, (color >> 8) & 0xff, color & 0xff);
    } else if (palette && color < 8) {
      codes.push((foreground ? 30 : 40) + color);
    } else if (palette && color < 16) {
      codes.push((foreground ? 90 : 100) + color - 8);
    } else if (palette) {
      codes.push(base, 5, color);
    }
  }

  function terminalSnapshotCellStyle(cell) {
    const codes = [0];
    if (cell.isBold()) codes.push(1);
    if (cell.isDim()) codes.push(2);
    if (cell.isItalic()) codes.push(3);
    if (cell.isUnderline()) codes.push(4);
    if (cell.isInverse()) codes.push(7);
    if (cell.isInvisible()) codes.push(8);
    if (cell.isStrikethrough()) codes.push(9);
    if (cell.isOverline()) codes.push(53);
    appendTerminalSnapshotColor(codes, cell, true);
    appendTerminalSnapshotColor(codes, cell, false);
    return `\x1b[${codes.join(';')}m`;
  }

  function safeTerminalSnapshotText(text) {
    return [...text].map((character) => {
      const code = character.codePointAt(0);
      return code < 0x20 || (code >= 0x7f && code <= 0x9f) ? ' ' : character;
    }).join('');
  }

  function captureTerminalSnapshot(source) {
    const buffer = source.buffer.active;
    let ansi = '\x1b[?25l\x1b[?7l\x1b[2J\x1b[H';
    for (let row = 0; row < source.rows; row += 1) {
      const line = buffer.getLine(buffer.viewportY + row);
      ansi += `\x1b[${row + 1};1H`;
      if (!line) continue;
      let style = '';
      for (let column = 0; column < source.cols;) {
        const cell = line.getCell(column);
        if (!cell) {
          ansi += ' ';
          column += 1;
          continue;
        }
        const width = Math.max(1, cell.getWidth());
        if (cell.getWidth() === 0) {
          column += 1;
          continue;
        }
        const nextStyle = terminalSnapshotCellStyle(cell);
        if (nextStyle !== style) {
          ansi += nextStyle;
          style = nextStyle;
        }
        const characters = safeTerminalSnapshotText(cell.getChars());
        ansi += characters || ' '.repeat(width);
        column += width;
      }
    }
    ansi += '\x1b[0m\x1b[H';
    return { ansi, cols: source.cols, rows: source.rows };
  }

  function renderMobileSnapshotButton(active) {
    const pane = selectedPaneTerminal();
    const open = pane?.snapshot !== undefined;
    mobileSnapshotButton.dataset.active = String(open);
    mobileSnapshotButton.setAttribute('aria-pressed', String(open));
    mobileSnapshotButton.setAttribute(
      'aria-label', open ? 'Close terminal snapshot' : 'Open terminal snapshot',
    );
    mobileSnapshotButton.title = open ? 'Close snapshot' : 'Terminal snapshot';
    mobileSnapshotButton.disabled = !pane || (active && !open);
    for (const icon of mobileSnapshotButton.querySelectorAll('[data-snapshot-icon]')) {
      icon.toggleAttribute(
        'hidden', icon.dataset.snapshotIcon !== (open ? 'close' : 'capture'),
      );
    }
  }

  function syncTerminalSnapshotControls() {
    const active = [...paneTerminals.values()].some((pane) => pane.snapshot !== undefined);
    paneGrid.dataset.snapshotActive = String(active);
    panesView.dataset.snapshotActive = String(active);
    for (const button of paneMobileBar.querySelectorAll('button')) {
      if (button !== paneBrowse && button !== mobileSnapshotButton) button.disabled = active;
    }
    renderMobileSnapshotButton(active);
  }

  function closeTerminalSnapshot(pane, syncControls = true) {
    const snapshot = pane?.snapshot;
    if (!snapshot) return;
    pane.snapshot = undefined;
    pane.tile.dataset.snapshot = 'false';
    pane.terminal.element?.ownerDocument.getSelection()?.removeAllRanges();
    snapshot.terminal.dispose();
    snapshot.view.remove();
    syncPaneKeyboardHelper(pane);
    if (syncControls) syncTerminalSnapshotControls();
  }

  function openTerminalSnapshot(pane) {
    if (!pane || pane.snapshot) return;
    pane.cancelTouchScroll?.();
    stopMobileKeyRepeat();
    resetMobileModifiers();
    pane.terminal.clearSelection();
    pane.terminal.element?.ownerDocument.getSelection()?.removeAllRanges();
    paneKeyboardHelper(pane)?.blur();

    const capture = captureTerminalSnapshot(pane.terminal);
    const view = document.createElement('div');
    view.className = 'pane-snapshot-view';
    view.setAttribute('role', 'region');
    view.setAttribute('aria-label', 'Terminal snapshot. Select text to copy.');
    const snapshotHost = document.createElement('div');
    snapshotHost.className = 'pane-snapshot-terminal';
    view.append(snapshotHost);
    pane.tile.append(view);
    pane.tile.dataset.snapshot = 'true';

    const snapshotTerminal = new Terminal({
      cols: capture.cols,
      rows: capture.rows,
      cursorBlink: false,
      customGlyphs: true,
      disableStdin: true,
      fontFamily: terminalFontFamily,
      fontSize: pane.terminal.options.fontSize,
      fontWeight: pane.terminal.options.fontWeight,
      fontWeightBold: pane.terminal.options.fontWeightBold,
      letterSpacing: pane.terminal.options.letterSpacing,
      lineHeight: pane.terminal.options.lineHeight,
      logLevel: 'error',
      rightClickSelectsWord: true,
      scrollback: 0,
      theme: terminalTheme,
    });
    pane.snapshot = { terminal: snapshotTerminal, view };
    syncTerminalSnapshotControls();
    syncPaneKeyboardHelper(pane);
    snapshotTerminal.open(snapshotHost);
    if (snapshotTerminal.textarea) {
      snapshotTerminal.textarea.readOnly = true;
      snapshotTerminal.textarea.tabIndex = -1;
      snapshotTerminal.textarea.blur();
    }
    snapshotTerminal.write(capture.ansi, () => {
      requestAnimationFrame(() => {
        if (pane.snapshot?.terminal === snapshotTerminal) view.dataset.ready = 'true';
      });
    });
  }

  function toggleTerminalSnapshot(pane) {
    if (pane?.snapshot) closeTerminalSnapshot(pane);
    else openTerminalSnapshot(pane);
  }

  function renderMobileMouseMode() {
    const mode = mobileMouseMode ? 'on' : 'off';
    mobileMouseModeButton.dataset.mode = mode;
    mobileMouseModeButton.setAttribute('aria-pressed', String(mobileMouseMode));
    mobileMouseModeButton.setAttribute(
      'aria-label',
      mobileMouseMode
        ? 'Mouse input on. Deactivate mouse input.'
        : 'Mouse input off. Activate mouse input.',
    );
    mobileMouseModeButton.title = mobileMouseMode ? 'Mouse input on' : 'Mouse input off';
    for (const icon of mobileMouseModeButton.querySelectorAll('[data-mode-icon]')) {
      icon.toggleAttribute('hidden', icon.dataset.modeIcon !== mode);
    }
    paneGrid.dataset.mouseReporting = mode;
    for (const pane of paneTerminals.values()) syncPaneKeyboardHelper(pane);
  }

  function setMobileMouseMode(enabled, announce = true) {
    mobileMouseMode = enabled;
    for (const pane of paneTerminals.values()) pane.cancelTouchScroll?.();
    terminal?.clearSelection();
    terminal?.element?.ownerDocument.getSelection()?.removeAllRanges();
    renderMobileMouseMode();
    if (announce) {
      showBrowserToast(
        enabled
          ? 'Mouse input on; live Paste is off'
          : 'Mouse input off; Paste is available',
      );
    }
    if (enabled && !mobileQuery.matches) terminal?.focus();
  }

  function renderMobileKeyboardLock() {
    mobileKeyboardLockButton.dataset.locked = String(mobileKeyboardLocked);
    mobileKeyboardLockButton.setAttribute('aria-pressed', String(mobileKeyboardLocked));
    mobileKeyboardLockButton.setAttribute(
      'aria-label',
      mobileKeyboardLocked
        ? 'Keyboard locked. Deactivate keyboard lock.'
        : 'Keyboard unlocked. Activate keyboard lock.',
    );
    mobileKeyboardLockButton.title = mobileKeyboardLocked
      ? 'Keyboard locked' : 'Keyboard unlocked';
    for (const icon of mobileKeyboardLockButton.querySelectorAll('[data-lock-icon]')) {
      icon.toggleAttribute(
        'hidden',
        icon.dataset.lockIcon !== (mobileKeyboardLocked ? 'locked' : 'unlocked'),
      );
    }
    paneGrid.dataset.keyboardLocked = String(mobileKeyboardLocked);
    for (const pane of paneTerminals.values()) syncPaneKeyboardHelper(pane);
  }

  function setMobileKeyboardLocked(locked, announce = true) {
    mobileKeyboardLocked = locked;
    if (locked) {
      for (const pane of paneTerminals.values()) resetMobileBackspaceAcceleration(pane);
    }
    renderMobileKeyboardLock();
    if (announce) {
      showBrowserToast(locked ? 'Keyboard locked' : 'Keyboard unlocked');
    }
  }

  function terminalMousePoint(pane, clientX, clientY) {
    const screen = pane.terminal.element?.querySelector('.xterm-screen');
    if (!screen) return undefined;
    const bounds = screen.getBoundingClientRect();
    return {
      column: Math.max(1, Math.min(
        pane.terminal.cols,
        Math.floor(((clientX - bounds.left) / Math.max(1, bounds.width)) * pane.terminal.cols) + 1,
      )),
      row: Math.max(1, Math.min(
        pane.terminal.rows,
        Math.floor(((clientY - bounds.top) / Math.max(1, bounds.height)) * pane.terminal.rows) + 1,
      )),
    };
  }

  // Mobile input has one owner for each event type:
  // - native iOS text and replacement input: this application;
  // - native caret movement and composition inside owned text: this application;
  // - other composition and explicit terminal keys: xterm;
  // - Backspace: the keydown path when present, with beforeinput as the
  //   keydown-free fallback; only native repeat events drive acceleration.
  // The native marker initializes WebKit repeat state. It is never terminal data.
  function paneKeyboardHelper(pane) {
    return pane?.terminal.textarea;
  }

  function resetMobileBackspaceSentinelState(pane) {
    pane.mobileBackspaceSentinel = false;
    pane.mobileBackspaceSentinelInsertion = false;
    pane.mobileBackspaceSentinelNative = false;
  }

  function clearMobileBackspaceSentinel(pane) {
    if (!pane?.mobileBackspaceSentinel) return;
    resetMobileBackspaceSentinelState(pane);
    const helper = paneKeyboardHelper(pane);
    if (!helper) return;
    if (helper.value.startsWith(MOBILE_BACKSPACE_SENTINEL)) {
      const start = Math.max(0, helper.selectionStart - MOBILE_BACKSPACE_SENTINEL.length);
      const end = Math.max(0, helper.selectionEnd - MOBILE_BACKSPACE_SENTINEL.length);
      const direction = helper.selectionDirection;
      helper.value = helper.value.slice(MOBILE_BACKSPACE_SENTINEL.length);
      helper.setSelectionRange(start, end, direction);
    }
  }

  function insertNativeMobileBackspaceSentinel(pane) {
    const helper = paneKeyboardHelper(pane);
    if (!helper || document.activeElement !== helper) return false;
    pane.mobileBackspaceSentinel = true;
    pane.mobileBackspaceSentinelInsertion = true;
    pane.mobileBackspaceSentinelNative = false;
    helper.value = '';
    helper.setSelectionRange(0, 0);
    let inserted = false;
    try {
      inserted = document.execCommand('insertText', false, MOBILE_BACKSPACE_SENTINEL);
    } catch (_error) {
      inserted = false;
    } finally {
      pane.mobileBackspaceSentinelInsertion = false;
    }
    if (helper.value !== MOBILE_BACKSPACE_SENTINEL) {
      helper.value = MOBILE_BACKSPACE_SENTINEL;
    }
    helper.setSelectionRange(helper.value.length, helper.value.length);
    pane.mobileBackspaceSentinelNative = inserted;
    return inserted;
  }

  function ensureMobileBackspaceSentinel(pane) {
    const helper = paneKeyboardHelper(pane);
    if (!helper || pane.mobilePredictionText) return;
    if (pane.mobileBackspaceSentinel) {
      if (document.activeElement === helper && !pane.mobileBackspaceSentinelNative) {
        insertNativeMobileBackspaceSentinel(pane);
      } else if (!helper.value) {
        helper.value = MOBILE_BACKSPACE_SENTINEL;
        helper.setSelectionRange(helper.value.length, helper.value.length);
      }
      return;
    }
    if (helper.value) return;
    pane.mobileBackspaceSentinel = true;
    if (!insertNativeMobileBackspaceSentinel(pane)) {
      helper.value = MOBILE_BACKSPACE_SENTINEL;
      helper.setSelectionRange(helper.value.length, helper.value.length);
    }
  }

  function setMobilePredictionAttributes(helper, enabled) {
    if (!helper) return;
    helper.setAttribute('autocorrect', enabled ? 'on' : 'off');
    helper.setAttribute('autocapitalize', 'none');
    helper.setAttribute('autocomplete', 'off');
    helper.setAttribute('spellcheck', enabled ? 'true' : 'false');
    if (enabled) helper.setAttribute('enterkeyhint', 'send');
    else helper.removeAttribute('enterkeyhint');
  }

  function clearMobilePredictionState(pane, clearHelper = false) {
    if (!pane) return;
    pane.mobilePredictionText = '';
    pane.mobilePredictionCursor = 0;
    pane.mobilePredictionConfirmed = false;
    pane.mobilePredictionInvalidated = false;
    pane.mobilePredictionComposition = undefined;
    const helper = paneKeyboardHelper(pane);
    setMobilePredictionAttributes(helper, false);
    if (clearHelper) {
      pane.mobileBackspacePreservedHelper = false;
      resetMobileBackspaceSentinelState(pane);
      if (helper) helper.value = '';
    }
  }

  function preserveMobileHelperForBackspace(pane) {
    if (!pane) return;
    pane.mobilePredictionText = '';
    pane.mobilePredictionCursor = 0;
    pane.mobilePredictionConfirmed = false;
    pane.mobilePredictionInvalidated = false;
    pane.mobilePredictionComposition = undefined;
    const helper = paneKeyboardHelper(pane);
    if (helper && !helper.value && pane.mobileBackspaceSentinel) {
      helper.value = MOBILE_BACKSPACE_SENTINEL;
      helper.setSelectionRange(helper.value.length, helper.value.length);
    }
    pane.mobileBackspacePreservedHelper = Boolean(helper?.value);
    setMobilePredictionAttributes(helper, false);
  }

  function clearPreservedMobileBackspaceHelper(pane) {
    if (!pane?.mobileBackspacePreservedHelper) return;
    pane.mobileBackspacePreservedHelper = false;
    resetMobileBackspaceSentinelState(pane);
    const helper = paneKeyboardHelper(pane);
    if (helper) helper.value = '';
  }

  function syncMobilePredictionFromTerminal(pane) {
    const text = pane?.mobilePredictionText;
    if (!text || pane.mobilePredictionComposition) return;
    // Keep the native baseline to suppress duplicate input after output changes.
    // Once confirmed text disappears, that baseline cannot authorize cursor
    // movement or deletion. A pending, not-yet-confirmed edit is not a mismatch.
    const confirmed = terminalHasEditableText(pane.terminal, text);
    if (pane.mobilePredictionConfirmed && !confirmed) pane.mobilePredictionInvalidated = true;
    pane.mobilePredictionConfirmed = !pane.mobilePredictionInvalidated && confirmed;
  }

  function prepareMobilePredictionFocus(pane) {
    const helper = paneKeyboardHelper(pane);
    if (!helper || !iosKeyboard || !mobileQuery.matches) return;
    syncMobilePredictionFromTerminal(pane);
    setMobilePredictionAttributes(helper, pane.mobilePredictionConfirmed);
    if (
      pane.mobilePredictionConfirmed
      && pane.mobilePredictionText
      && !pane.mobilePredictionComposition
    ) {
      resetMobileBackspaceSentinelState(pane);
      if (helper.value !== pane.mobilePredictionText) {
        helper.value = pane.mobilePredictionText;
        helper.setSelectionRange(pane.mobilePredictionCursor, pane.mobilePredictionCursor);
      }
    } else {
      ensureMobileBackspaceSentinel(pane);
    }
  }

  function preserveMobilePredictionBeforeBlur(pane) {
    const helper = paneKeyboardHelper(pane);
    if (!helper || !iosKeyboard || !mobileQuery.matches) return;
    // Safari may queue selectionchange behind blur. Send that final movement
    // before xterm clears the helper, then retain the last sent caret position.
    followMobileCaret(pane, true);
    if (pane.mobileBackspacePreservedHelper) {
      clearPreservedMobileBackspaceHelper(pane);
      return;
    }
    if (pane.mobileBackspaceSentinel) {
      clearMobileBackspaceSentinel(pane);
      return;
    }
    if (!helper.value) return;
    if (helper.value.length > MOBILE_PREDICTION_TEXT_LIMIT) {
      clearMobilePredictionState(pane);
      return;
    }
    const confirmed = !pane.mobilePredictionInvalidated
      && helper.value === pane.mobilePredictionText
      && terminalHasEditableText(pane.terminal, helper.value);
    if (!confirmed) {
      // xterm clears the helper after this blur listener. Do not retain an
      // unconfirmed baseline that cannot be restored on the next focus.
      clearMobilePredictionState(pane);
      return;
    }
    pane.mobilePredictionText = helper.value;
    pane.mobilePredictionConfirmed = true;
  }

  function mobileHelperCaret(helper) {
    return helper.selectionDirection === 'backward'
      ? helper.selectionStart : helper.selectionEnd;
  }

  function followMobileCaret(pane, beforeBlur = false) {
    const helper = paneKeyboardHelper(pane);
    if (!iosKeyboard || !mobileQuery.matches || mobileKeyboardLocked
      || !helper || (!beforeBlur && document.activeElement !== helper)
      || !paneAcceptsInput(pane, false) || pane.snapshot
      || pane.mobilePredictionComposition || pane.mobileBackspaceSentinel
      || pane.mobileBackspacePreservedHelper || !pane.mobilePredictionConfirmed
      || helper.value !== pane.mobilePredictionText
      || helper.selectionStart !== helper.selectionEnd) return;
    const cursor = helper.selectionStart;
    if (cursor === pane.mobilePredictionCursor) return;
    const data = terminalCaretInput(
      pane.mobilePredictionText, pane.mobilePredictionCursor, cursor,
      pane.terminal.modes?.applicationCursorKeysMode,
    );
    if (!data) return;
    if (sendMobilePaneKeyboardData(pane, data)) pane.mobilePredictionCursor = cursor;
  }

  function handleMobileCaretSelection() {
    const pane = paneForKeyboardTarget(document.activeElement);
    if (pane) followMobileCaret(pane);
  }

  function applyMobileTextValue(pane, text, cursor, useModifiers = false) {
    const edit = terminalTextInputDelta(
      pane.mobilePredictionText, text, pane.mobilePredictionCursor, cursor,
      pane.terminal.modes?.applicationCursorKeysMode,
    );
    if (!edit) return false;
    // After unrelated terminal output, retain the native baseline but send only
    // newly inserted text. Never move or delete against the invalidated range.
    const input = pane.mobilePredictionInvalidated ? edit.inserted : edit.data;
    if (input) {
      const data = useModifiers && input === edit.inserted
        ? applyMobileModifiers(input) : input;
      if (!sendMobilePaneKeyboardData(pane, data)) return false;
      if (data !== input) {
        // Modifier keys can send controls or different text. Do not keep the
        // native helper value as an editable model of that terminal input.
        clearMobilePredictionState(pane, true);
        return true;
      }
    }
    pane.mobilePredictionText = text;
    pane.mobilePredictionCursor = cursor;
    pane.mobilePredictionConfirmed = !pane.mobilePredictionInvalidated && Boolean(text)
      && terminalHasEditableText(pane.terminal, text);
    return true;
  }

  function handleMobileTextInput(pane, event) {
    const helper = paneKeyboardHelper(pane);
    if (
      !helper
      || event.target !== helper
      || !iosKeyboard
      || !mobileQuery.matches
      || event.inputType === 'insertFromPaste'
    ) return false;
    if (
      pane.mobileBackspacePreservedHelper
      && (
        event.inputType === 'deleteContentBackward'
        || event.inputType === 'deleteWordBackward'
      )
    ) {
      ensureMobileBackspaceSentinel(pane);
      return false;
    }
    if (pane.mobilePredictionComposition) {
      event.stopImmediatePropagation();
      return true;
    }
    if (
      event.isComposing
      || event.inputType === 'insertCompositionText'
      || event.inputType === 'deleteCompositionText'
      || event.inputType === 'insertFromComposition'
    ) return false;
    if (
      event.inputType
      && event.inputType !== 'insertText'
      && event.inputType !== 'insertReplacementText'
    ) return false;

    clearMobileBackspaceSentinel(pane);
    if (pane.mobileBackspacePreservedHelper) {
      pane.mobileBackspacePreservedHelper = false;
      pane.mobilePredictionText = '';
      pane.mobilePredictionCursor = 0;
      pane.mobilePredictionConfirmed = false;
      pane.mobilePredictionInvalidated = false;
    }

    event.stopImmediatePropagation();
    if (!paneAcceptsInput(pane, false) || mobileKeyboardLocked || pane.snapshot) return true;
    if (!applyMobileTextValue(pane, helper.value, mobileHelperCaret(helper), true)
      && helper.value.length > MOBILE_PREDICTION_TEXT_LIMIT) {
      clearMobilePredictionState(pane, true);
      showBrowserToast('Mobile input was too long');
    }
    return true;
  }

  function sendMobilePredictionReplacement(pane, state, replacement) {
    const helper = paneKeyboardHelper(pane);
    if (!helper || !paneAcceptsInput(pane, false)) return false;
    const result = terminalPredictionReplacement({
      text: state.text,
      selectionStart: state.selectionStart,
      selectionEnd: state.selectionEnd,
      replacement,
      cursor: pane.mobilePredictionCursor,
      applicationCursorKeys: pane.terminal.modes?.applicationCursorKeysMode,
      editable: state.editable
        && terminalHasEditableText(pane.terminal, state.text),
    });
    if (!result || !setActivePane(pane.streamId)) return false;
    pane.terminal.clearSelection();
    sendInput(result.data);
    pane.mobilePredictionText = result.text;
    pane.mobilePredictionCursor = result.cursor;
    pane.mobilePredictionConfirmed = false;
    helper.value = result.text;
    helper.setSelectionRange(result.cursor, result.cursor);
    return true;
  }

  function handleMobilePredictionCompositionStart(event) {
    if (!iosKeyboard || !mobileQuery.matches || mobileKeyboardLocked) return;
    const pane = paneForKeyboardTarget(event.target);
    const helper = paneKeyboardHelper(pane);
    if (!pane || !helper) return;
    clearPreservedMobileBackspaceHelper(pane);
    clearMobileBackspaceSentinel(pane);
    followMobileCaret(pane);
    const selectionStart = helper.selectionStart;
    const selectionEnd = helper.selectionEnd;
    if (
      !pane.mobilePredictionText || helper.value !== pane.mobilePredictionText
      || !Number.isInteger(selectionStart) || !Number.isInteger(selectionEnd)
      || !paneAcceptsInput(pane, false) || pane.snapshot
    ) {
      clearMobilePredictionState(pane);
      return;
    }
    pane.mobilePredictionComposition = {
      text: helper.value,
      selectionStart,
      selectionEnd,
    };
    event.stopImmediatePropagation();
  }

  function handleMobilePredictionCompositionUpdate(event) {
    const pane = paneForKeyboardTarget(event.target);
    if (pane?.mobilePredictionComposition) event.stopImmediatePropagation();
  }

  function handleMobilePredictionCompositionEnd(event) {
    const pane = paneForKeyboardTarget(event.target);
    const helper = paneKeyboardHelper(pane);
    const state = pane?.mobilePredictionComposition;
    if (!pane || !helper || !state) return;
    event.stopImmediatePropagation();
    const eventData = event.data;
    setTimeout(() => {
      if (pane.mobilePredictionComposition !== state) return;
      pane.mobilePredictionComposition = undefined;
      if (!paneAcceptsInput(pane, false) || pane.snapshot || mobileKeyboardLocked) return;
      if (!terminalHasEditableText(pane.terminal, state.text)) {
        pane.mobilePredictionInvalidated = true;
        pane.mobilePredictionConfirmed = false;
      }
      // Native composition can revise the full helper value. Read it only
      // after the final input event. Do not move its selection during IME use.
      if (helper.value !== state.text) {
        applyMobileTextValue(pane, helper.value, mobileHelperCaret(helper));
      } else if (typeof eventData === 'string' && eventData) {
        const text = state.text.slice(0, state.selectionStart)
          + eventData + state.text.slice(state.selectionEnd);
        const cursor = state.selectionStart + eventData.length;
        if (applyMobileTextValue(pane, text, cursor)) {
          helper.value = text;
          helper.setSelectionRange(cursor, cursor);
        }
      }
    }, 0);
  }

  function noteMobilePredictionTerminalData(pane, data) {
    if (!iosKeyboard || !mobileQuery.matches) return;
    if (data === '\x7f' || data === '\x1b\x7f') {
      preserveMobileHelperForBackspace(pane);
      return;
    }
    if (/[\x00-\x1f\x7f]/u.test(data)) clearMobilePredictionState(pane, true);
  }

  function resetPaneKeyboardHelper(pane) {
    const helper = paneKeyboardHelper(pane);
    if (!helper) return;
    helper.classList.remove('mobile-keyboard-target', 'mobile-native-paste-target');
    setMobilePredictionAttributes(helper, false);
    for (const property of [
      'left', 'top', 'width', 'height', 'fontSize', 'lineHeight', 'zIndex',
    ]) {
      helper.style[property] = '';
    }
  }

  function syncPaneKeyboardHelper(pane) {
    const helper = paneKeyboardHelper(pane);
    if (!helper) return;
    const mobileTarget = mobileQuery.matches && paneCompact && viewMode === 'panes';
    if (!mobileTarget) return;
    const enabled = !mobileKeyboardLocked
      && pane.mode === 'control'
      && !pane.closed
      && !pane.snapshot;
    if (!enabled) {
      if (document.activeElement === helper) helper.blur();
      resetPaneKeyboardHelper(pane);
      return;
    }
    syncMobilePredictionFromTerminal(pane);
    setMobilePredictionAttributes(helper, pane.mobilePredictionConfirmed);
    const screen = pane.terminal.element?.querySelector('.xterm-screen');
    const screenBounds = screen?.getBoundingClientRect();
    const terminalBounds = pane.terminal.element?.getBoundingClientRect();
    if (!screenBounds || !terminalBounds || pane.terminal.cols < 1 || pane.terminal.rows < 1) return;
    const cellWidth = screenBounds.width / pane.terminal.cols;
    const cellHeight = screenBounds.height / pane.terminal.rows;
    const column = Math.max(0, Math.min(pane.terminal.cols - 1, pane.terminal.buffer.active.cursorX));
    const row = Math.max(0, Math.min(pane.terminal.rows - 1, pane.terminal.buffer.active.cursorY));
    const nativePasteTarget = !mobileMouseMode;
    const targetWidth = nativePasteTarget ? screenBounds.width : 1;
    const targetHeight = Math.max(1, cellHeight);
    const screenLeft = screenBounds.left - terminalBounds.left;
    const screenTop = screenBounds.top - terminalBounds.top;
    const cursorLeft = screenLeft + column * cellWidth;
    const cursorTop = screenTop + row * cellHeight;
    const targetLeft = nativePasteTarget ? screenLeft : cursorLeft;
    helper.classList.add('mobile-keyboard-target');
    helper.classList.toggle('mobile-native-paste-target', nativePasteTarget);
    helper.style.left = `${targetLeft.toFixed(3)}px`;
    helper.style.top = `${cursorTop.toFixed(3)}px`;
    helper.style.width = `${targetWidth.toFixed(3)}px`;
    helper.style.height = `${targetHeight.toFixed(3)}px`;
    helper.style.fontSize = `${IOS_KEYBOARD_FONT_SIZE}px`;
    helper.style.lineHeight = `${Math.max(1, cellHeight).toFixed(3)}px`;
    helper.style.zIndex = nativePasteTarget ? '10' : '4';
    helper.dataset.cursorColumn = String(column);
    helper.dataset.cursorRow = String(row);
  }

  function focusPaneKeyboard(pane) {
    if (mobileKeyboardLocked || pane.mode !== 'control' || pane.closed || pane.snapshot) return false;
    if (!setActivePane(pane.streamId)) return false;
    syncPaneKeyboardHelper(pane);
    prepareMobilePredictionFocus(pane);
    pane.terminal.focus();
    return document.activeElement === paneKeyboardHelper(pane);
  }

  function paneForKeyboardTarget(target) {
    return [...paneTerminals.values()].find(
      (candidate) => paneKeyboardHelper(candidate) === target,
    );
  }

  function sendMobilePaneKeyboardData(pane, data) {
    if (!paneAcceptsInput(pane)) return false;
    if (!setActivePane(pane.streamId)) {
      showBrowserToast('Waiting for input to reach the previous pane');
      return false;
    }
    pane.terminal.clearSelection();
    sendInput(data);
    return true;
  }

  function resetMobileBackspaceAcceleration(pane) {
    clearTimeout(pane.mobileBackspaceResetTimer);
    pane.mobileBackspaceResetTimer = undefined;
    pane.mobileBackspaceCount = 0;
    pane.suppressDeletionBeforeInputUntil = 0;
  }

  function resetMobilePaneInput(pane, blur = false) {
    resetMobileBackspaceAcceleration(pane);
    clearMobilePredictionState(pane, true);
    const helper = paneKeyboardHelper(pane);
    if (blur && document.activeElement === helper) helper.blur();
  }

  function handleMobileTerminalKeyDown(event) {
    if (!iosKeyboard || !mobileQuery.matches || mobileKeyboardLocked) return;
    const pane = paneForKeyboardTarget(event.target);
    if (!pane) return;
    if (event.key !== 'Backspace') {
      // iOS can report software-keyboard edits as Unidentified/keyCode 229.
      // Keep the native marker until beforeinput identifies the operation.
      if (event.key === 'Unidentified' || event.keyCode === 229) {
        if (!event.isComposing) event.stopImmediatePropagation();
        return;
      }
      clearPreservedMobileBackspaceHelper(pane);
      clearMobileBackspaceSentinel(pane);
      resetMobileBackspaceAcceleration(pane);
      return;
    }
    if (event.isComposing) return;
    followMobileCaret(pane);
    if (event.altKey || event.ctrlKey || event.metaKey) {
      resetMobileBackspaceAcceleration(pane);
      pane.suppressDeletionBeforeInputUntil = performance.now()
        + MOBILE_BACKSPACE_BEFORE_INPUT_SUPPRESSION_MS;
      return;
    }
    clearTimeout(pane.mobileBackspaceResetTimer);
    pane.mobileBackspaceCount = (pane.mobileBackspaceCount || 0) + 1;
    pane.mobileBackspaceResetTimer = setTimeout(
      () => resetMobileBackspaceAcceleration(pane),
      MOBILE_BACKSPACE_RESET_MS,
    );
    const data = terminalDataForRepeatedMobileBackspace(pane.mobileBackspaceCount);
    if (!data || !event.cancelable) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    pane.suppressDeletionBeforeInputUntil = performance.now()
      + MOBILE_BACKSPACE_BEFORE_INPUT_SUPPRESSION_MS;
    preserveMobileHelperForBackspace(pane);
    sendMobilePaneKeyboardData(pane, data);
  }

  function handleMobileTerminalBeforeInput(event) {
    if (!mobileQuery.matches || mobileKeyboardLocked) return;
    const pane = paneForKeyboardTarget(event.target);
    if (!pane) return;
    if (pane.mobileBackspaceSentinelInsertion) {
      event.stopImmediatePropagation();
      return;
    }
    if (event.isComposing || !event.cancelable) return;
    // Safari can deliver the final caret change just before the text edit,
    // before its queued selectionchange event reaches this document.
    followMobileCaret(pane);
    if (
      iosKeyboard
      && event.inputType !== 'deleteContentBackward'
      && event.inputType !== 'deleteWordBackward'
    ) {
      clearPreservedMobileBackspaceHelper(pane);
      // Keep a fresh native marker until Safari applies the text edit. Changing
      // the helper during beforeinput can discard the first swipe insertion.
      resetMobileBackspaceAcceleration(pane);
    }
    if (iosKeyboard && event.inputType === 'insertReplacementText') {
      const helper = paneKeyboardHelper(pane);
      const state = {
        text: pane.mobilePredictionText,
        selectionStart: helper?.selectionStart,
        selectionEnd: helper?.selectionEnd,
        editable: pane.mobilePredictionConfirmed
          && helper?.value === pane.mobilePredictionText,
      };
      if (!sendMobilePredictionReplacement(pane, state, event.data)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const data = terminalDataForBeforeInput(event.inputType);
    if (!data) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    preserveMobileHelperForBackspace(pane);
    if ((pane.suppressDeletionBeforeInputUntil || 0) >= performance.now()) {
      pane.suppressDeletionBeforeInputUntil = 0;
      return;
    }
    sendMobilePaneKeyboardData(pane, data);
  }

  function terminalMouseButtonCode(button, motion = false) {
    return button
      + Number(mobileModifierState.shift) * 4
      + Number(mobileModifierState.alt) * 8
      + Number(mobileModifierState.control) * 16
      + Number(motion) * 32;
  }

  function queuePaneMouseClick(pane, buttonCode, point) {
    // Herdr's terminal.input path follows the live viewport before it writes
    // bytes. Keep atomic clicks structured so the server can use pane.send_text.
    const operation = {
      kind: 'pane-mouse',
      ready: true,
      streamId: pane.streamId,
      buttonCode,
      column: point.column,
      row: point.row,
      action: 'click',
    };
    try {
      queuePendingMouseMotion();
      drainInput();
      appendInputOperation(operation);
      drainInput();
      return true;
    } catch (error) {
      showBrowserToast(error.message);
      return false;
    }
  }

  function queuePaneTextPaste(pane, text) {
    const normalized = normalizeTerminalPasteText(text);
    if (!normalized) return false;
    const retainedBytes = inputEncoder.encode(normalized).byteLength;
    if (retainedBytes > MAX_PANE_TEXT_PASTE_BYTES) {
      showBrowserToast('Text paste must be smaller than 512 KiB');
      return false;
    }
    if (!paneAcceptsInput(pane) || !setActivePane(pane.streamId)) return false;
    pane.terminal.clearSelection();
    try {
      queuePendingMouseMotion();
      drainInput();
      appendInputOperation({
        kind: 'pane-paste',
        ready: true,
        retainedBytes,
        streamId: pane.streamId,
        text: normalized,
      });
      drainInput();
      return true;
    } catch (error) {
      showBrowserToast(error.message);
      return false;
    }
  }

  function sendTerminalMouseReport(pane, button, clientX, clientY, final = 'M', motion = false) {
    if (!paneAcceptsInput(pane, !motion) || !setActivePane(pane.streamId)) return false;
    const point = terminalMousePoint(pane, clientX, clientY);
    if (!point) return false;
    const code = terminalMouseButtonCode(button, motion);
    sendInput(`\x1b[<${code};${point.column};${point.row}${final}`);
    return true;
  }

  function sendTerminalMouseClick(pane, button, clientX, clientY) {
    if (!paneAcceptsInput(pane) || !setActivePane(pane.streamId)) return false;
    const point = terminalMousePoint(pane, clientX, clientY);
    if (!point) return false;
    const code = terminalMouseButtonCode(button);
    return queuePaneMouseClick(pane, code, point);
  }

  function sendMobileArrow(direction) {
    sendMobileNavigationKey(direction);
  }

  function stopMobileKeyRepeat() {
    clearTimeout(mobileKeyRepeatDelay);
    clearInterval(mobileKeyRepeatInterval);
    mobileKeyRepeatDelay = undefined;
    mobileKeyRepeatInterval = undefined;
  }

  function startMobileKeyRepeat(send) {
    stopMobileKeyRepeat();
    send();
    mobileKeyRepeatDelay = setTimeout(() => {
      mobileKeyRepeatInterval = setInterval(send, 70);
    }, 350);
  }

  function bindRepeatableMobileKey(button, send) {
    let retainedKeyboardPane;
    const captureKeyboardPane = () => {
      const pane = selectedPaneTerminal();
      if (pane && document.activeElement === paneKeyboardHelper(pane)) {
        retainedKeyboardPane = pane;
      }
    };
    const restoreKeyboardPane = () => {
      const pane = retainedKeyboardPane;
      retainedKeyboardPane = undefined;
      if (pane && pane === selectedPaneTerminal()) focusPaneKeyboard(pane);
    };

    button.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      captureKeyboardPane();
      event.preventDefault();
      if (event.pointerType !== 'touch') {
        try {
          button.setPointerCapture(event.pointerId);
        } catch (_) {
          // Some assistive input does not expose a capturable pointer.
        }
      }
      startMobileKeyRepeat(send);
    });
    button.addEventListener('touchstart', (event) => {
      captureKeyboardPane();
      event.preventDefault();
    }, { passive: false });
    for (const eventName of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      button.addEventListener(eventName, (event) => {
        if (event.cancelable) event.preventDefault();
        stopMobileKeyRepeat();
        restoreKeyboardPane();
      });
    }
    for (const eventName of ['touchend', 'touchcancel']) {
      button.addEventListener(eventName, (event) => {
        event.preventDefault();
        stopMobileKeyRepeat();
        captureKeyboardPane();
        restoreKeyboardPane();
      }, { passive: false });
    }
    button.addEventListener('mousedown', (event) => event.preventDefault());
    button.addEventListener('click', (event) => {
      event.preventDefault();
      if (event.detail === 0) send();
      captureKeyboardPane();
      restoreKeyboardPane();
    });
  }

  function syncTerminalGridMetrics(activeTerminal) {
    const screen = activeTerminal.element?.querySelector('.xterm-screen');
    if (!screen || activeTerminal.cols < 1 || activeTerminal.rows < 1) return;
    const bounds = screen.getBoundingClientRect();
    const cellWidth = bounds.width / activeTerminal.cols;
    const cellHeight = bounds.height / activeTerminal.rows;
    if (!Number.isFinite(cellWidth) || !Number.isFinite(cellHeight)) return;
    const root = document.documentElement;
    root.style.setProperty('--terminal-cell-width', `${cellWidth.toFixed(4)}px`);
    root.style.setProperty('--terminal-cell-height', `${cellHeight.toFixed(4)}px`);
    root.style.setProperty('--terminal-column-2', `${(cellWidth * 2).toFixed(4)}px`);
    root.style.setProperty('--terminal-column-3', `${(cellWidth * 3).toFixed(4)}px`);
    root.style.setProperty('--terminal-column-4', `${(cellWidth * 4).toFixed(4)}px`);
    root.style.setProperty('--terminal-column-6', `${(cellWidth * 6).toFixed(4)}px`);
    root.style.setProperty('--terminal-row-2', `${(cellHeight * 2).toFixed(4)}px`);
    root.style.setProperty('--terminal-row-3', `${(cellHeight * 3).toFixed(4)}px`);
    root.style.setProperty('--terminal-row-4', `${(cellHeight * 4).toFixed(4)}px`);
  }

  function fitPaneTerminal(pane) {
    pane.fitAddon.fit();
    syncTerminalGridMetrics(pane.terminal);
    let remainder = 0;
    if (paneCompact) {
      const screen = pane.terminal.element?.querySelector('.xterm-screen');
      const screenBounds = screen?.getBoundingClientRect();
      const hostBounds = pane.host.getBoundingClientRect();
      if (screenBounds && pane.terminal.rows > 0) {
        const cellHeight = screenBounds.height / pane.terminal.rows;
        const availableHeight = hostBounds.height + (pane.absorbedRemainder || 0);
        if (Number.isFinite(cellHeight) && cellHeight > 0) {
          const completeRows = Math.floor((availableHeight + 0.25) / cellHeight);
          remainder = Math.max(0, availableHeight - completeRows * cellHeight);
          if (remainder < 0.5 || cellHeight - remainder < 0.5) remainder = 0;
        }
      }
    }
    if (Math.abs(remainder - (pane.absorbedRemainder || 0)) < 0.25) {
      syncPaneKeyboardHelper(pane);
      return;
    }
    pane.absorbedRemainder = remainder;
    if (remainder) {
      pane.tile.style.setProperty('--pane-terminal-remainder', `${remainder.toFixed(4)}px`);
    } else {
      pane.tile.style.removeProperty('--pane-terminal-remainder');
    }
    pane.fitAddon.fit();
    syncTerminalGridMetrics(pane.terminal);
    syncPaneKeyboardHelper(pane);
  }

  function physicalModifiedEnterData(event) {
    if (event.key !== 'Enter' || event.isComposing) return undefined;
    const physicalModifiers = {
      shift: event.shiftKey === true,
      alt: event.altKey === true,
      control: event.ctrlKey === true,
      meta: event.metaKey === true,
    };
    if (!Object.values(physicalModifiers).some(Boolean)) return undefined;
    const modifiers = mobileQuery.matches
      ? {
          shift: physicalModifiers.shift || mobileModifierState.shift,
          alt: physicalModifiers.alt || mobileModifierState.alt,
          control: physicalModifiers.control || mobileModifierState.control,
          meta: physicalModifiers.meta,
        }
      : physicalModifiers;
    return terminalDataForModifiedEnter(modifiers);
  }

  function paneTerminalKeyHandler(event) {
    if (
      iosKeyboard
      && mobileQuery.matches
      && !mobileKeyboardLocked
      && !event.isComposing
      && !event.ctrlKey
      && !event.altKey
      && !event.metaKey
      && (event.type === 'keydown' || event.type === 'keypress')
      && (event.key?.length === 1 || event.key === 'Unidentified' || event.keyCode === 229)
    ) {
      // The helper input event owns native mobile text. Do not also let xterm
      // send a printable keydown or keypress, including the Space key.
      return false;
    }
    if (event.type !== 'keydown') return true;
    const modifiedEnter = physicalModifiedEnterData(event);
    if (modifiedEnter) {
      event.preventDefault();
      sendInput(modifiedEnter);
      return false;
    }
    if (!event.metaKey) return true;
    const commandKey = { ArrowLeft: '\x01', ArrowRight: '\x05', Backspace: '\x15' }[event.key];
    if (!commandKey) return true;
    event.preventDefault();
    sendInput(commandKey);
    return false;
  }

  function createPaneTerminal(paneRecord, layoutPane, streamId, area) {
    const tile = document.createElement('section');
    tile.className = 'pane-tile';
    tile.dataset.active = String(paneRecord.pane_id === selectedPane);
    tile.setAttribute('aria-label', paneDisplayLabel(paneRecord));
    if (!paneCompact && area.width > 0 && area.height > 0) {
      const left = ((layoutPane.rect.x - area.x) / area.width) * 100;
      const top = ((layoutPane.rect.y - area.y) / area.height) * 100;
      const width = (layoutPane.rect.width / area.width) * 100;
      const height = (layoutPane.rect.height / area.height) * 100;
      tile.style.left = `${left}%`;
      tile.style.top = `${top}%`;
      tile.style.width = `${width}%`;
      tile.style.height = `${height}%`;
    }
    const title = document.createElement('div');
    title.className = 'pane-title';
    const label = document.createElement('span');
    label.className = 'pane-title-label';
    label.textContent = paneDisplayLabel(paneRecord);
    const state = document.createElement('span');
    state.className = 'pane-title-state';
    state.textContent = paneRecord.agent_status === 'unknown' ? '' : paneRecord.agent_status || '';
    const connection = document.createElement('button');
    connection.type = 'button';
    connection.className = 'pane-connection';
    connection.addEventListener('pointerdown', (event) => event.preventDefault());
    connection.addEventListener('click', (event) => {
      event.stopPropagation();
      if (connectionIndicator.dataset.state === 'disconnected') requestReconnect();
    });
    title.append(label, state, connection);
    const host = document.createElement('div');
    host.className = 'pane-terminal';
    tile.append(title, host);
    paneGrid.append(tile);

    const paneTerminal = new Terminal({
      cursorBlink: true,
      customGlyphs: true,
      fontFamily: terminalFontFamily,
      fontSize: paneCompact ? mobileTerminalFontSize : DEFAULT_TERMINAL_FONT_SIZE,
      logLevel: 'error',
      rightClickSelectsWord: false,
      scrollback: 0,
      theme: terminalTheme,
    });
    const paneFitAddon = new FitAddon();
    paneTerminal.loadAddon(paneFitAddon);
    paneTerminal.open(host);
    paneTerminal.parser.registerOscHandler(52, handleOsc52);
    paneTerminal.attachCustomKeyEventHandler(paneTerminalKeyHandler);
    paneTerminal.element.addEventListener('contextmenu', (event) => {
      if (mobileQuery.matches && !mobileMouseMode) {
        if (mobileKeyboardLocked) event.stopImmediatePropagation();
        return;
      }
      event.preventDefault();
    }, true);
    const record = {
      paneId: paneRecord.pane_id,
      streamId,
      terminal: paneTerminal,
      fitAddon: paneFitAddon,
      host,
      tile,
      label,
      state,
      connection,
      mode: 'control',
      closed: false,
      awaitingFull: true,
      absorbedRemainder: 0,
      mobileBackspaceCount: 0,
      suppressDeletionBeforeInputUntil: 0,
      mobilePredictionText: '',
      mobilePredictionCursor: 0,
      mobilePredictionConfirmed: false,
      mobilePredictionInvalidated: false,
      mobilePredictionComposition: undefined,
      mobileBackspacePreservedHelper: false,
      mobileBackspaceSentinel: false,
      mobileBackspaceSentinelInsertion: false,
      mobileBackspaceSentinelNative: false,
    };
    paneTerminal.element.addEventListener('focus', (event) => {
      if (event.target === paneKeyboardHelper(record)) prepareMobilePredictionFocus(record);
    }, true);
    paneTerminal.element.addEventListener('blur', (event) => {
      if (event.target === paneKeyboardHelper(record)) preserveMobilePredictionBeforeBlur(record);
    }, true);
    paneTerminal.element.addEventListener('paste', (event) => {
      if (mobileQuery.matches && mobileMouseMode) {
        event.preventDefault();
        event.stopImmediatePropagation();
        showBrowserToast('Turn mouse input off to Paste');
        return;
      }
      const image = clipboardImageFromPaste(event);
      if (image) {
        event.preventDefault();
        event.stopImmediatePropagation();
        void sendClipboardImage(image, record).catch(
          () => showBrowserToast('Could not read clipboard image'),
        );
        return;
      }
      const text = event.clipboardData?.getData('text/plain');
      if (!text) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      clearMobilePredictionState(record, true);
      queuePaneTextPaste(record, text);
    }, true);
    paneTerminal.element.addEventListener('input', (event) => {
      if (
        event.target === paneKeyboardHelper(record)
        && record.mobileBackspaceSentinelInsertion
      ) {
        event.stopImmediatePropagation();
        return;
      }
      if (handleMobileTextInput(record, event)) return;
      if (
        event.inputType !== 'insertFromPaste'
        || event.target !== paneKeyboardHelper(record)
      ) return;
      clearPreservedMobileBackspaceHelper(record);
      clearMobileBackspaceSentinel(record);
      const text = event.target.value;
      if (!text) return;
      event.stopImmediatePropagation();
      event.target.value = '';
      clearMobilePredictionState(record);
      if (mobileMouseMode) {
        showBrowserToast('Turn mouse input off to Paste');
        return;
      }
      queuePaneTextPaste(record, text);
    }, true);
    paneTerminals.set(streamId, record);
    syncPaneConnectionStatus();
    syncTerminalSnapshotControls();
    host.addEventListener('wheel', (event) => {
      record.cancelTouchScroll?.();
      event.preventDefault();
      event.stopImmediatePropagation();
      setActivePane(streamId);
      if (mobileQuery.matches && mobileMouseMode) {
        const horizontal = Math.abs(event.deltaX) > Math.abs(event.deltaY);
        const delta = horizontal ? event.deltaX : event.deltaY;
        const button = horizontal ? (delta < 0 ? 66 : 67) : (delta < 0 ? 64 : 65);
        const count = Math.max(1, Math.min(3, Math.round(Math.abs(delta) / 80)));
        for (let index = 0; index < count; index += 1) {
          sendTerminalMouseReport(record, button, event.clientX, event.clientY);
        }
        return;
      }
      paneTerminal.clearSelection();
      const bounds = host.getBoundingClientRect();
      const column = Math.max(0, Math.min(
        paneTerminal.cols - 1,
        Math.floor(((event.clientX - bounds.left) / Math.max(1, bounds.width)) * paneTerminal.cols),
      ));
      const row = Math.max(0, Math.min(
        paneTerminal.rows - 1,
        Math.floor(((event.clientY - bounds.top) / Math.max(1, bounds.height)) * paneTerminal.rows),
      ));
      sendPaneScroll(
        record,
        event.deltaY < 0 ? 'up' : 'down',
        Math.max(1, Math.min(12, Math.round(Math.abs(event.deltaY) / 40))),
        column,
        row,
      );
    }, { passive: false, capture: true });
    let touchPointerY;
    let touchPointerX;
    let touchStartY;
    let touchStartX;
    let touchScrollDistance = 0;
    let touchScrollVelocity = 0;
    let touchScrollActive = false;
    let touchMousePressed = false;
    let touchMouseGesture;
    let touchMoved = false;
    let touchLongPressFired = false;
    let touchStartTime;
    let touchLongPressTimer;
    let touchScrollTime;
    let touchScrollAnimation;
    let preserveClipboardFocusUntil = 0;
    let suppressClipboardClickUntil = 0;

    const cancelTouchLongPress = () => {
      clearTimeout(touchLongPressTimer);
      touchLongPressTimer = undefined;
    };

    const cancelTouchScroll = () => {
      if (touchScrollAnimation !== undefined) cancelAnimationFrame(touchScrollAnimation);
      cancelTouchLongPress();
      if (touchMousePressed && touchPointerX !== undefined && touchPointerY !== undefined) {
        sendTerminalMouseReport(record, 0, touchPointerX, touchPointerY, 'm');
      }
      touchMousePressed = false;
      touchMouseGesture = undefined;
      touchScrollAnimation = undefined;
      touchPointerY = undefined;
      touchPointerX = undefined;
      touchStartY = undefined;
      touchStartX = undefined;
      touchScrollActive = false;
      touchMoved = false;
      touchLongPressFired = false;
      touchScrollDistance = 0;
      touchScrollVelocity = 0;
      touchScrollTime = undefined;
      touchStartTime = undefined;
      if (preserveClipboardFocusUntil === Number.POSITIVE_INFINITY) {
        preserveClipboardFocusUntil = performance.now()
          + MOBILE_NATIVE_MENU_CLICK_SUPPRESSION_MS;
      }
    };
    record.cancelTouchScroll = cancelTouchScroll;

    const applyTouchScrollDistance = (distance, clientX, clientY) => {
      touchScrollDistance += distance;
      touchPointerX = clientX;
      const screen = paneTerminal.element?.querySelector('.xterm-screen');
      const screenHeight = screen?.getBoundingClientRect().height || host.clientHeight;
      const lineHeight = Math.max(1, screenHeight / Math.max(1, paneTerminal.rows));
      const lines = Math.min(12, Math.floor(Math.abs(touchScrollDistance) / lineHeight));
      if (lines < 1) return true;
      const direction = touchScrollDistance > 0 ? 'up' : 'down';
      touchScrollDistance -= Math.sign(touchScrollDistance) * lines * lineHeight;
      const bounds = host.getBoundingClientRect();
      const x = Number.isFinite(clientX) ? clientX : bounds.left + bounds.width / 2;
      const y = Number.isFinite(clientY) ? clientY : bounds.top + bounds.height / 2;
      const column = Math.max(0, Math.min(
        paneTerminal.cols - 1,
        Math.floor(((x - bounds.left) / Math.max(1, bounds.width)) * paneTerminal.cols),
      ));
      const row = Math.max(0, Math.min(
        paneTerminal.rows - 1,
        Math.floor(((y - bounds.top) / Math.max(1, bounds.height)) * paneTerminal.rows),
      ));
      return sendPaneScroll(record, direction, lines, column, row);
    };

    const startTouchMomentum = () => {
      if (
        reducedMotionQuery.matches
        || Math.abs(touchScrollVelocity) < MOBILE_SCROLL_STOP_VELOCITY
      ) {
        touchScrollDistance = 0;
        return;
      }
      let velocity = Math.max(
        -MOBILE_SCROLL_MAX_VELOCITY,
        Math.min(MOBILE_SCROLL_MAX_VELOCITY, touchScrollVelocity),
      );
      let previousTime = performance.now();
      const step = (now) => {
        const elapsed = Math.min(32, Math.max(1, now - previousTime));
        previousTime = now;
        if (!applyTouchScrollDistance(velocity * elapsed, touchPointerX, touchPointerY)) {
          cancelTouchScroll();
          return;
        }
        velocity *= Math.exp(-elapsed / MOBILE_SCROLL_DECAY_MS);
        if (Math.abs(velocity) < MOBILE_SCROLL_STOP_VELOCITY) {
          touchScrollAnimation = undefined;
          touchScrollDistance = 0;
          return;
        }
        touchScrollAnimation = requestAnimationFrame(step);
      };
      touchScrollAnimation = requestAnimationFrame(step);
    };

    host.addEventListener('touchstart', (event) => {
      if (!mobileQuery.matches || event.touches.length !== 1) return;
      const touch = event.touches[0];
      if (!mobileMouseMode) {
        const retainKeyboard = document.activeElement === paneKeyboardHelper(record);
        cancelTouchScroll();
        touchPointerY = touch.clientY;
        touchPointerX = touch.clientX;
        touchStartY = touch.clientY;
        touchStartX = touch.clientX;
        touchScrollActive = true;
        touchMoved = false;
        touchScrollTime = performance.now();
        touchStartTime = touchScrollTime;
        if (retainKeyboard) preserveClipboardFocusUntil = Number.POSITIVE_INFINITY;
        setActivePane(streamId);
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      cancelTouchScroll();
      touchPointerY = touch.clientY;
      touchPointerX = touch.clientX;
      touchStartY = touch.clientY;
      touchStartX = touch.clientX;
      touchScrollActive = true;
      touchMoved = false;
      touchScrollTime = performance.now();
      touchStartTime = touchScrollTime;
      setActivePane(streamId);
      touchLongPressTimer = setTimeout(() => {
        if (!touchScrollActive || touchPointerX === undefined || touchPointerY === undefined) return;
        touchLongPressTimer = undefined;
        touchLongPressFired = true;
        touchScrollDistance = 0;
        touchScrollVelocity = 0;
        if (sendTerminalMouseClick(record, 2, touchPointerX, touchPointerY)) {
          navigator.vibrate?.(12);
          showBrowserToast('Right-click sent');
        }
      }, MOBILE_LONG_PRESS_MS);
    }, { passive: false, capture: true });
    host.addEventListener('touchmove', (event) => {
      if (!touchScrollActive || touchPointerY === undefined) return;
      if (event.touches.length !== 1) {
        cancelTouchScroll();
        return;
      }
      const touch = event.touches[0];
      const moved = touchStartX !== undefined && touchStartY !== undefined
        && Math.hypot(touch.clientX - touchStartX, touch.clientY - touchStartY)
          > MOBILE_LONG_PRESS_MOVE_PX;
      if (!mobileMouseMode && !moved) return;
      event.preventDefault();
      event.stopPropagation();
      if (touchLongPressFired) return;
      if (moved) {
        cancelTouchLongPress();
        if (!touchMoved && !mobileMouseMode) paneTerminal.clearSelection();
        touchMoved = true;
        if (mobileMouseMode && touchMouseGesture === undefined) {
          const held = performance.now() - (touchStartTime || performance.now());
          const horizontal = Math.abs(touch.clientX - touchStartX);
          const vertical = Math.abs(touch.clientY - touchStartY);
          touchMouseGesture = held < MOBILE_MOUSE_DRAG_HOLD_MS && vertical >= horizontal
            ? 'scroll'
            : 'drag';
        }
      }
      if (mobileMouseMode && touchMouseGesture !== 'scroll') {
        if (touchMouseGesture === 'drag' || touchMousePressed) {
          if (!touchMousePressed && touchStartX !== undefined && touchStartY !== undefined) {
            touchMousePressed = sendTerminalMouseReport(record, 0, touchStartX, touchStartY);
          }
          if (touchMousePressed) {
            sendTerminalMouseReport(record, 0, touch.clientX, touch.clientY, 'M', true);
          }
        }
        touchPointerY = touch.clientY;
        touchPointerX = touch.clientX;
        return;
      }
      const now = performance.now();
      const elapsed = Math.max(1, now - touchScrollTime);
      const distance = touch.clientY - touchPointerY;
      const instantVelocity = Math.max(
        -MOBILE_SCROLL_MAX_VELOCITY,
        Math.min(MOBILE_SCROLL_MAX_VELOCITY, distance / elapsed),
      );
      touchScrollVelocity = Math.sign(instantVelocity) !== Math.sign(touchScrollVelocity)
        ? instantVelocity
        : touchScrollVelocity * 0.65 + instantVelocity * 0.35;
      touchPointerY = touch.clientY;
      touchPointerX = touch.clientX;
      touchScrollTime = now;
      if (!applyTouchScrollDistance(distance, touch.clientX, touch.clientY)) cancelTouchScroll();
    }, { passive: false, capture: true });
    host.addEventListener('touchend', (event) => {
      cancelTouchLongPress();
      if (!touchScrollActive || touchPointerY === undefined) return;
      if (touchLongPressFired) {
        event.preventDefault();
        event.stopPropagation();
        cancelTouchScroll();
        return;
      }
      const now = performance.now();
      const touchDuration = now - (touchStartTime || now);
      if (!mobileMouseMode) {
        if (!touchMoved) {
          if (touchDuration >= MOBILE_LONG_PRESS_MS) {
            suppressClipboardClickUntil = now + MOBILE_NATIVE_MENU_CLICK_SUPPRESSION_MS;
          }
          cancelTouchScroll();
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        suppressClipboardClickUntil = now + MOBILE_NATIVE_MENU_CLICK_SUPPRESSION_MS;
        if (preserveClipboardFocusUntil === Number.POSITIVE_INFINITY) {
          preserveClipboardFocusUntil = now + MOBILE_NATIVE_MENU_CLICK_SUPPRESSION_MS;
        }
        touchScrollActive = false;
        touchStartY = undefined;
        touchStartX = undefined;
        touchScrollTime = undefined;
        touchStartTime = undefined;
        startTouchMomentum();
        return;
      }
      if (!touchMoved && touchDuration < MOBILE_LONG_PRESS_MS) {
        event.preventDefault();
        event.stopPropagation();
        sendTerminalMouseClick(record, 0, touchPointerX, touchPointerY);
        focusPaneKeyboard(record);
        cancelTouchScroll();
        return;
      }
      if (!touchMoved) {
        event.preventDefault();
        event.stopPropagation();
        cancelTouchScroll();
        return;
      }
      if (mobileMouseMode && touchMouseGesture !== 'scroll') {
        event.preventDefault();
        event.stopPropagation();
        if (touchMousePressed) {
          sendTerminalMouseReport(record, 0, touchPointerX, touchPointerY, 'm');
          touchMousePressed = false;
        }
        cancelTouchScroll();
        return;
      }
      touchScrollActive = false;
      touchStartY = undefined;
      touchStartX = undefined;
      touchScrollTime = undefined;
      touchStartTime = undefined;
      startTouchMomentum();
    }, { passive: false, capture: true });
    host.addEventListener('touchcancel', cancelTouchScroll, { passive: true, capture: true });
    host.addEventListener('click', (event) => {
      if (!mobileQuery.matches || mobileMouseMode || event.button !== 0) return;
      if (performance.now() < suppressClipboardClickUntil) {
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      focusPaneKeyboard(record);
    }, true);

    let pointerMouseButton;
    host.addEventListener('pointerdown', (event) => {
      if (!mobileQuery.matches || !mobileMouseMode || event.pointerType !== 'mouse') return;
      if (event.button < 0 || event.button > 2) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (record.mode !== 'control') {
        showBrowserToast('This pane is read-only');
        return;
      }
      pointerMouseButton = event.button;
      try {
        host.setPointerCapture(event.pointerId);
      } catch (_) {
        // Pointer capture is optional for synthetic and assistive input.
      }
      sendTerminalMouseReport(record, pointerMouseButton, event.clientX, event.clientY);
    }, true);
    host.addEventListener('pointermove', (event) => {
      if (!mobileQuery.matches || !mobileMouseMode || event.pointerType !== 'mouse') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      sendTerminalMouseReport(
        record,
        pointerMouseButton ?? 3,
        event.clientX,
        event.clientY,
        'M',
        true,
      );
    }, true);
    const releasePointerMouse = (event) => {
      if (!mobileQuery.matches || !mobileMouseMode || event.pointerType !== 'mouse') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (pointerMouseButton !== undefined) {
        sendTerminalMouseReport(record, pointerMouseButton, event.clientX, event.clientY, 'm');
      }
      pointerMouseButton = undefined;
    };
    host.addEventListener('pointerup', releasePointerMouse, true);
    host.addEventListener('pointercancel', releasePointerMouse, true);
    for (const eventName of ['mousedown', 'mousemove', 'mouseup']) {
      host.addEventListener(eventName, (event) => {
        if (!mobileQuery.matches) return;
        const preserveClipboardFocus = !mobileMouseMode
          && eventName === 'mousedown'
          && document.activeElement === paneKeyboardHelper(record)
          && (event.button === 2 || performance.now() < preserveClipboardFocusUntil);
        if (mobileMouseMode || preserveClipboardFocus) event.preventDefault();
        event.stopImmediatePropagation();
      }, true);
    }
    paneTerminal.onData((data) => {
      const terminalData = applyMobileModifiers(data);
      noteMobilePredictionTerminalData(record, terminalData);
      if (!paneAcceptsInput(record)) return;
      if (!setActivePane(streamId)) {
        showBrowserToast('Waiting for input to reach the previous pane');
        return;
      }
      paneTerminal.clearSelection();
      sendInput(terminalData);
    });
    paneTerminal.element.addEventListener('focusin', () => setActivePane(streamId));
    tile.addEventListener('pointerdown', () => setActivePane(streamId));
    fitPaneTerminal(record);
    return record;
  }

  function sendPaneResizes() {
    if (viewMode !== 'panes' || resizeQueued) return;
    resizeQueued = true;
    requestAnimationFrame(() => {
      resizeQueued = false;
      for (const pane of paneTerminals.values()) {
        fitPaneTerminal(pane);
        if (socket?.readyState === WebSocket.OPEN && outputFlow?.paneMode) {
          pane.awaitingFull = true;
          socket.send(JSON.stringify({
            type: 'pane-resize',
            stream_id: pane.streamId,
            cols: pane.terminal.cols,
            rows: pane.terminal.rows,
          }));
        }
      }
    });
  }

  function buildPaneGrid() {
    paneCompact = mobileQuery.matches;
    const layout = paneLayoutForTab(selectedTab);
    const maps = navigationMaps();
    if (!layout || !layout.panes?.length) throw new Error('The selected tab has no pane layout');
    const available = layout.panes.filter((pane) => maps.paneById.has(pane.pane_id));
    if (!available.length) throw new Error('The selected tab has no available panes');
    if (!available.some((pane) => pane.pane_id === selectedPane)) {
      selectedPane = layout.focused_pane_id || available[0].pane_id;
    }
    const visible = paneCompact
      ? available.filter((pane) => pane.pane_id === selectedPane)
      : available;
    const selectedVisible = visible.length ? visible : [available[0]];
    disposePaneTerminals();
    let streamId = 1;
    for (const layoutPane of selectedVisible) {
      const paneRecord = maps.paneById.get(layoutPane.pane_id);
      const record = createPaneTerminal(paneRecord, layoutPane, streamId, layout.area);
      currentPaneRequests.push({
        stream_id: streamId,
        pane_id: paneRecord.pane_id,
        cols: record.terminal.cols,
        rows: record.terminal.rows,
      });
      streamId += 1;
    }
    const selectedRecord = [...paneTerminals.values()].find((pane) => pane.paneId === selectedPane)
      || paneTerminals.values().next().value;
    if (selectedRecord) {
      selectedPane = selectedRecord.paneId;
      setActivePane(selectedRecord.streamId, false);
      if (!paneCompact) selectedRecord.terminal.focus();
    }
    updatePaneLocation();
    renderPaneNavigation();
  }

  function decodePaneFrame(buffer) {
    if (buffer.byteLength < PANE_FRAME_HEADER_BYTES) throw new Error('pane frame is truncated');
    const view = new DataView(buffer);
    if (view.getUint32(0) !== PANE_FRAME_MAGIC) throw new Error('pane frame has an invalid header');
    const flags = view.getUint8(16);
    if (flags & ~PANE_FRAME_KNOWN_FLAGS) throw new Error('pane frame has invalid flags');
    const bytes = new Uint8Array(buffer, PANE_FRAME_HEADER_BYTES);
    if (bytes.byteLength > MAX_PANE_FRAME_BYTES) {
      throw new Error('pane frame is too large');
    }
    return {
      streamId: view.getUint32(4),
      seq: view.getBigUint64(8),
      full: (flags & PANE_FRAME_FLAG_FULL) !== 0,
      compressed: (flags & PANE_FRAME_FLAG_DEFLATE) !== 0,
      width: view.getUint16(17),
      height: view.getUint16(19),
      bytes,
    };
  }

  async function decompressPaneFrame(frame) {
    if (!frame.compressed) return frame;
    if (typeof DecompressionStream !== 'function') {
      throw new Error('This browser cannot decompress pane output');
    }
    const stream = new Blob([frame.bytes]).stream().pipeThrough(
      new DecompressionStream('deflate'),
    );
    const reader = stream.getReader();
    const chunks = [];
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_PANE_FRAME_BYTES) {
        await reader.cancel('decompressed pane frame is too large');
        throw new Error('decompressed pane frame is too large');
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    frame.bytes = bytes;
    frame.compressed = false;
    return frame;
  }

  function acknowledgePaneFrame(frame, flow) {
    if (flow !== outputFlow || flow.socket.readyState !== WebSocket.OPEN) return;
    flow.socket.send(JSON.stringify({
      type: 'pane-output-ack', stream_id: frame.streamId, seq: frame.seq.toString(),
    }));
  }

  function queuePaneFrame(frame, flow) {
    receivedFrames += 1;
    const pane = paneTerminals.get(frame.streamId);
    if (!pane) return;
    const matchingSize = frame.width === pane.terminal.cols
      && frame.height === pane.terminal.rows;
    if (!matchingSize) {
      pane.awaitingFull = true;
      acknowledgePaneFrame(frame, flow);
      if (flow === outputFlow && flow.socket.readyState === WebSocket.OPEN) {
        flow.socket.send(JSON.stringify({
          type: 'pane-resize',
          stream_id: pane.streamId,
          cols: pane.terminal.cols,
          rows: pane.terminal.rows,
        }));
      }
      return;
    }
    if (pane.awaitingFull && !frame.full) {
      acknowledgePaneFrame(frame, flow);
      return;
    }
    if (frame.full && pane.awaitingFull) pane.terminal.reset();
    pane.awaitingFull = false;
    pane.terminal.write(frame.bytes, () => {
      syncPaneKeyboardHelper(pane);
      if (pane.scrollAwaitingFrame) {
        pane.scrollAwaitingFrame = false;
        flushPaneScroll(pane);
      }
      acknowledgePaneFrame(frame, flow);
      if (pane.paneId === selectedPane && pane.mode === 'control' && !pane.closed) {
        flow.inputReady = true;
        scheduleInputDrain();
      }
    });
  }

  function startPaneWebSocket(backend) {
    for (const pane of paneTerminals.values()) pane.awaitingFull = true;
    let opened = false;
    let attached = false;
    const nextSocket = new WebSocket(wsUrl(backend.id));
    const flow = {
      socket: nextSocket,
      paneMode: true,
      attached: false,
      inputReady: false,
      frameChain: Promise.resolve(),
    };
    clearOutputFlow();
    outputFlow = flow;
    socket = nextSocket;
    nextSocket.binaryType = 'arraybuffer';
    clearTimeout(connectTimer);
    connectTimer = setTimeout(() => {
      if (socket === nextSocket && !opened) {
        nextSocket.close();
        setStatus('Panes mode needs WebSocket support', 'disconnected');
      }
    }, 3000);
    nextSocket.onopen = () => {
      if (socket !== nextSocket) return nextSocket.close();
      opened = true;
      clearTimeout(connectTimer);
      setStatus('Attaching panes…');
      nextSocket.send(JSON.stringify({
        type: 'panes.attach',
        tab_id: selectedTab,
        panes: currentPaneRequests,
        compression: paneDeflateSupported ? 'deflate' : undefined,
      }));
    };
    nextSocket.onmessage = (event) => {
      if (socket !== nextSocket) return;
      if (typeof event.data === 'string') {
        const message = JSON.parse(event.data);
        if (message.type === 'panes-attached') {
          attached = true;
          flow.attached = true;
          for (const stream of message.streams || []) {
            const pane = paneTerminals.get(stream.stream_id);
            if (pane) {
              pane.mode = stream.mode || 'control';
              pane.terminal.options.disableStdin = pane.mode === 'observe';
              if (pane.mode === 'observe') resetMobilePaneInput(pane, true);
              updatePaneTitle(pane);
            }
          }
          clearTimeout(connectTimer);
          connectTimer = undefined;
          clearTimeout(reconnectStableTimer);
          reconnectStableTimer = setTimeout(() => {
            if (socket === nextSocket) reconnectAttempts = 0;
          }, 30_000);
          setStatus('Connected', 'connected');
          const active = [...paneTerminals.values()].find((pane) => pane.paneId === selectedPane);
          if (active) nextSocket.send(JSON.stringify({ type: 'pane-active', stream_id: active.streamId }));
        } else if (message.type === 'pane-mode') {
          const pane = paneTerminals.get(message.stream_id);
          if (pane) {
            pane.mode = message.mode;
            pane.terminal.options.disableStdin = message.mode === 'observe';
            if (pane.mode === 'observe') resetMobilePaneInput(pane, true);
            updatePaneTitle(pane);
            if (pane.paneId === selectedPane && message.mode === 'observe') {
              flow.inputReady = false;
              const discardedInput = inputBuffer.length || inputOperations.length;
              clearPendingMouseMotion();
              inputBuffer.clear();
              inputOperations.length = 0;
              pendingPaneActivation = undefined;
              if (discardedInput) {
                showBrowserToast('Queued input was discarded because this pane is read-only');
              }
            }
          }
        } else if (message.type === 'pane-closed') {
          const pane = paneTerminals.get(message.stream_id);
          if (pane) {
            pane.closed = true;
            pane.terminal.options.disableStdin = true;
            clearTimeout(pane.scrollFlushTimer);
            pane.scrollFlushTimer = undefined;
            pane.pendingScrollDelta = 0;
            resetMobilePaneInput(pane, true);
            if (pane.paneId === selectedPane) {
              flow.inputReady = false;
              clearPendingMouseMotion();
              inputBuffer.clear();
              inputOperations.length = 0;
              pendingPaneActivation = undefined;
            }
            updatePaneTitle(pane);
          }
          showBrowserToast(message.reason || 'A pane stream closed');
        } else if (message.type === 'ping') {
          nextSocket.send(JSON.stringify({ type: 'pong' }));
        } else if (message.type === 'error') {
          showBrowserToast(message.message);
        }
        return;
      }
      flow.frameChain = flow.frameChain.then(async () => {
        const frame = decodePaneFrame(event.data);
        await decompressPaneFrame(frame);
        if (flow !== outputFlow || flow.socket.readyState !== WebSocket.OPEN) return;
        queuePaneFrame(frame, flow);
      }).catch((error) => {
        showBrowserToast(error.message);
        nextSocket.close();
      });
    };
    nextSocket.onclose = () => {
      if (socket !== nextSocket) return;
      clearTimeout(connectTimer);
      connectTimer = undefined;
      socket = undefined;
      clearOutputFlow(flow);
      if (attached && viewMode === 'panes') scheduleWebSocketReconnect(backend);
      else if (viewMode === 'panes') void recoverWebSocket(backend, 'panes');
    };
    nextSocket.onerror = () => {};
  }

  function queueTerminalOutput(bytes, flow) {
    receivedFrames += 1;
    const activeTerminal = terminal;
    if (!activeTerminal) return Promise.resolve(false);
    // Use xterm's supported write queue. It limits parser work per browser
    // task and gives the renderer an opportunity to paint between batches.
    return new Promise((resolve) => {
      activeTerminal.write(bytes, () => {
        if (terminal !== activeTerminal) {
          resolve(false);
          return;
        }
        if (flow) noteParsedOutput(flow, bytes.length);
        else {
          httpInputReady = true;
          scheduleInputDrain();
        }
        resolve(true);
      });
    });
  }

  function inputBytesBeforeOperation() {
    if (!inputOperations.length) return inputBuffer.length;
    return Math.max(0, inputOperations[0].offset - inputBuffer.consumedBytes);
  }

  function scheduleInputDrain(delay = 0) {
    if (inputDrainTimer !== undefined) return;
    inputDrainTimer = setTimeout(() => {
      inputDrainTimer = undefined;
      drainInput();
    }, delay);
  }

  function discardInputOperations(message) {
    if (!inputOperations.length) return;
    inputOperations.length = 0;
    showBrowserToast(message);
  }

  function drainWebSocketInput(activeSocket, flow) {
    if (!flow.inputReady) return;
    while (
      flow === outputFlow && flow.attached && activeSocket === socket
      && activeSocket.readyState === WebSocket.OPEN
      && activeSocket.bufferedAmount < INPUT_WEBSOCKET_HIGH_WATER_BYTES
    ) {
      const beforeOperation = inputBytesBeforeOperation();
      if (beforeOperation > 0) {
        const bytes = inputBuffer.peek(Math.min(INPUT_BATCH_BYTES, beforeOperation));
        activeSocket.send(bytes);
        inputBuffer.consume(bytes.length);
        continue;
      }

      const operation = inputOperations[0];
      if (!operation) break;
      if (!operation.ready) return;
      inputOperations.shift();
      if (operation.error) continue;
      if (operation.kind === 'pane-mouse') {
        activeSocket.send(JSON.stringify({
          type: 'pane-mouse',
          stream_id: operation.streamId,
          button_code: operation.buttonCode,
          column: operation.column,
          row: operation.row,
          action: operation.action,
        }));
        continue;
      }
      if (operation.kind === 'pane-paste') {
        activeSocket.send(JSON.stringify({
          type: 'pane-paste',
          stream_id: operation.streamId,
          text: operation.text,
        }));
        continue;
      }
      activeSocket.send(JSON.stringify({
        type: 'clipboard-image',
        stream_id: operation.streamId,
        extension: operation.extension,
        size: operation.bytes.byteLength,
      }));
      activeSocket.send(operation.bytes);
    }

    if (inputBuffer.length || inputOperations.length) scheduleInputDrain(8);
    else if (pendingPaneActivation !== undefined) {
      const streamId = pendingPaneActivation;
      pendingPaneActivation = undefined;
      setActivePane(streamId);
      if (!mobileQuery.matches) paneTerminals.get(streamId)?.terminal.focus();
    }
  }

  function abandonHttpSession(session, message) {
    if (sessionId !== session) return;
    const backend = currentBackend;
    sessionId = undefined;
    httpInputReady = false;
    clearTimeout(reconnectStableTimer);
    reconnectStableTimer = undefined;
    if (!authenticationReloading) setStatus(message, 'disconnected');
    fetch(apiUrl(`sessions/${session}`), { method: 'DELETE' }).catch(() => {});
    if (
      !authenticationReloading && backend && viewMode === 'full'
      && selectedBackendId() === backend.id
    ) {
      void recoverWebSocket(backend, 'full');
    }
  }

  function drainHttpInput() {
    if (!sessionId || !httpInputReady || httpInputInFlight || !inputBuffer.length) return;
    const beforeOperation = inputBytesBeforeOperation();
    if (beforeOperation <= 0) return;
    const length = Math.min(INPUT_BATCH_BYTES, beforeOperation);
    const dataBase64 = bytesToBase64(inputBuffer.peek(length));
    const session = sessionId;
    httpInputInFlight = true;
    void fetchWithTimeout(apiUrl(`sessions/${session}/input`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data_base64: dataBase64 }),
    }, 30_000).then((response) => {
      if (!response.ok) throw new Error(`terminal input failed (${response.status})`);
      if (sessionId === session) inputBuffer.consume(length);
    }).catch((error) => {
      abandonHttpSession(session, error.message);
    }).finally(() => {
      httpInputInFlight = false;
      if (sessionId === session && connectionIndicator.dataset.state !== 'disconnected') {
        scheduleInputDrain();
      }
    });
  }

  function drainInput() {
    const activeSocket = socket;
    const flow = outputFlow;
    if (activeSocket?.readyState === WebSocket.OPEN && flow?.socket === activeSocket) {
      drainWebSocketInput(activeSocket, flow);
      return;
    }
    drainHttpInput();
  }

  function clearPendingMouseMotion() {
    clearTimeout(mouseMotionTimer);
    mouseMotionTimer = undefined;
    pendingMouseMotion = undefined;
  }

  function queuePendingMouseMotion() {
    if (pendingMouseMotion === undefined) return false;
    const motion = pendingMouseMotion;
    pendingMouseMotion = undefined;
    inputBuffer.append(motion);
    clearTimeout(mouseMotionTimer);
    mouseMotionTimer = undefined;
    return true;
  }

  function flushPendingMouseMotion() {
    try {
      if (queuePendingMouseMotion()) drainInput();
    } catch (error) {
      showBrowserToast(error.message);
    }
  }

  function scheduleMouseMotion() {
    if (mouseMotionTimer !== undefined) return;
    mouseMotionTimer = setTimeout(() => {
      mouseMotionTimer = undefined;
      flushPendingMouseMotion();
    }, MOUSE_MOTION_INTERVAL_MS);
  }

  function schedulePaneStructureRebuild() {
    if (paneStructureRebuildTimer !== undefined) return;
    const rebuildWhenInputDrains = () => {
      paneStructureRebuildTimer = undefined;
      if (viewMode !== 'panes' || !currentBackend || !navigationSnapshot) return;
      if (
        inputBuffer.length || inputOperations.length || pendingMouseMotion !== undefined
        || (socket?.bufferedAmount || 0) > 0
      ) {
        paneStructureRebuildTimer = setTimeout(rebuildWhenInputDrains, 16);
        return;
      }
      rebuildPaneView();
    };
    paneStructureRebuildTimer = setTimeout(rebuildWhenInputDrains, 0);
  }

  function rebuildPaneView() {
    clearTimeout(paneStructureRebuildTimer);
    paneStructureRebuildTimer = undefined;
    if (viewMode !== 'panes' || !currentBackend || !navigationSnapshot) return;
    paneViewToken += 1;
    const oldSocket = socket;
    socket = undefined;
    oldSocket?.close();
    clearOutputFlow();
    clearTimeout(connectTimer);
    clearTimeout(reconnectTimer);
    connectTimer = undefined;
    reconnectTimer = undefined;
    clearPendingMouseMotion();
    inputBuffer.clear();
    inputOperations.length = 0;
    resizeObserver?.disconnect();
    try {
      buildPaneGrid();
      resizeObserver = new ResizeObserver(sendPaneResizes);
      resizeObserver.observe(paneGrid);
      startPaneWebSocket(currentBackend);
    } catch (error) {
      disposePaneTerminals();
      setStatus(error.message, 'disconnected');
    }
  }

  function selectPaneTarget(paneId) {
    const pane = navigationMaps().paneById.get(paneId);
    if (!pane) return;
    const tabChanged = pane.tab_id !== selectedTab;
    selectedWorkspace = pane.workspace_id;
    selectedTab = pane.tab_id;
    selectedPane = pane.pane_id;
    closeMobileSheet(false);
    updatePaneLocation();
    renderPaneNavigation();
    if (tabChanged || mobileQuery.matches) rebuildPaneView();
    else {
      const record = [...paneTerminals.values()].find((candidate) => candidate.paneId === paneId);
      if (record) {
        setActivePane(record.streamId);
        record.terminal.focus();
      }
    }
  }

  function selectPaneTab(tabId) {
    const tab = navigationMaps().tabById.get(tabId);
    if (!tab || tab.tab_id === selectedTab) {
      closeMobileSheet();
      return;
    }
    selectedWorkspace = tab.workspace_id;
    selectedTab = tab.tab_id;
    const layout = paneLayoutForTab(tabId);
    selectedPane = layout?.focused_pane_id
      || navigationMaps().panes.find((pane) => pane.tab_id === tabId)?.pane_id;
    closeMobileSheet(false);
    updatePaneLocation();
    renderPaneNavigation();
    rebuildPaneView();
  }

  function selectPaneWorkspace(workspaceId) {
    const maps = navigationMaps();
    const workspace = maps.workspaceById.get(workspaceId);
    if (!workspace) return;
    const tabId = workspace.active_tab_id
      || maps.tabs.find((tab) => tab.workspace_id === workspaceId)?.tab_id;
    if (!tabId) return;
    if (tabId === selectedTab) {
      selectedWorkspace = workspaceId;
      renderPaneNavigation();
      closeMobileSheet();
      return;
    }
    selectPaneTab(tabId);
  }

  async function attachPanes(backend) {
    const token = ++paneViewToken;
    clearReconnectState();
    reconnectAttempts = 0;
    receivedFrames = 0;
    inputBuffer.clear();
    inputOperations.length = 0;
    currentBackend = backend;
    viewMode = 'panes';
    httpFallbackStarting = false;
    picker.hidden = true;
    terminalView.hidden = false;
    terminalView.dataset.view = 'panes';
    terminalHost.hidden = true;
    panesView.hidden = false;
    fullModeButton.setAttribute('aria-pressed', 'false');
    panesModeButton.setAttribute('aria-pressed', 'true');
    backendName.textContent = backend.label;
    setStatus('Loading panes…');
    startNavigationPolling();
    try {
      await refreshNavigation();
      if (token !== paneViewToken || currentBackend?.id !== backend.id || viewMode !== 'panes') return;
      initializePaneSelection();
      renderPaneNavigation();
      buildPaneGrid();
      resizeObserver = new ResizeObserver(sendPaneResizes);
      resizeObserver.observe(paneGrid);
      startPaneWebSocket(backend);
    } catch (error) {
      if (token !== paneViewToken) return;
      setStatus(error.message, 'disconnected');
    }
  }

  function switchViewMode(mode) {
    if (mode === 'full' && mobileQuery.matches) return;
    if (!currentBackend || mode === viewMode) {
      closeMobileSheet(false);
      return;
    }
    const backend = currentBackend;
    closeMobileSheet(false);
    setViewLocation(mode);
    showPicker(false, false);
    openBackend(backend, false);
  }

  function showPicker(updateUrl = true, refresh = true) {
    closeMobileSheet(false);
    paneViewToken += 1;
    clearInterval(navigationTimer);
    navigationTimer = undefined;
    navigationSnapshot = undefined;
    navigationFingerprint = undefined;
    navigationRequest = undefined;
    mobileMouseMode = false;
    renderMobileMouseMode();
    clearTimeout(connectTimer);
    clearTimeout(reconnectTimer);
    clearTimeout(reconnectStableTimer);
    clearTimeout(reconnectReloadTimer);
    clearTimeout(paneStructureRebuildTimer);
    paneStructureRebuildTimer = undefined;
    connectTimer = undefined;
    reconnectTimer = undefined;
    reconnectStableTimer = undefined;
    reconnectReloadTimer = undefined;
    reconnectAttempts = 0;
    socket?.close();
    socket = undefined;
    clearOutputFlow();
    clearTimeout(inputDrainTimer);
    inputDrainTimer = undefined;
    clearPendingMouseMotion();
    inputBuffer.clear();
    inputOperations.length = 0;
    httpInputInFlight = false;
    httpInputReady = false;
    httpFallbackStarting = false;
    if (sessionId) fetch(apiUrl(`sessions/${sessionId}`), { method: 'DELETE' });
    sessionId = undefined;
    currentBackend = undefined;
    resizeObserver?.disconnect();
    resizeObserver = undefined;
    if (viewMode === 'panes') disposePaneTerminals();
    else terminal?.dispose();
    terminal = undefined;
    fitAddon = undefined;
    webglAddon = undefined;
    terminalHost.replaceChildren();
    terminalHost.hidden = false;
    panesView.hidden = true;
    paneGrid.replaceChildren();
    selectedWorkspace = undefined;
    selectedTab = undefined;
    selectedPane = undefined;
    terminalView.hidden = true;
    picker.hidden = false;
    if (updateUrl) setSelectedBackend(null);
    if (refresh) loadBackends(false);
  }

  function bytesToBase64(bytes) {
    let text = '';
    for (let offset = 0; offset < bytes.length; offset += 8192) {
      text += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    }
    return btoa(text);
  }

  function base64ToBytes(value) {
    const text = atob(value);
    const bytes = new Uint8Array(text.length);
    for (let index = 0; index < text.length; index += 1) bytes[index] = text.charCodeAt(index);
    return bytes;
  }

  function sendResize() {
    if (viewMode === 'panes') {
      sendPaneResizes();
      return;
    }
    if (!terminal || !fitAddon || resizeQueued) return;
    const activeTerminal = terminal;
    const activeFitAddon = fitAddon;
    resizeQueued = true;
    requestAnimationFrame(() => {
      resizeQueued = false;
      if (terminal !== activeTerminal || fitAddon !== activeFitAddon) return;
      activeFitAddon.fit();
      syncTerminalGridMetrics(activeTerminal);
      const resize = { type: 'resize', cols: activeTerminal.cols, rows: activeTerminal.rows };
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(resize));
      } else if (sessionId) {
        const session = sessionId;
        fetchWithTimeout(apiUrl(`sessions/${session}/resize`), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(resize),
        }).catch(() => abandonHttpSession(session, 'Disconnected'));
      }
    });
  }

  function sendInput(data) {
    try {
      if (viewMode === 'panes' && selectedPaneTerminal()?.snapshot) return;
      if (isDisposableMouseMotion(data)) {
        // Mouse tracking can produce input faster than a remote application
        // consumes it. Keep only the latest adjacent position. Do not let old
        // pointer positions delay keys, clicks, wheel events, or paste data.
        pendingMouseMotion = data;
        scheduleMouseMotion();
        return;
      }
      queuePendingMouseMotion();
      inputBuffer.append(data);
      // Drain in the input event task. A zero-delay timer can starve behind a
      // continuous stream of WebSocket output events.
      drainInput();
    } catch (error) {
      showBrowserToast(error.message);
    }
  }

  async function readTerminal(session) {
    while (sessionId === session) {
      try {
        const response = await fetchWithTimeout(
          apiUrl(`sessions/${session}/read`), { cache: 'no-store' }
        );
        if (!response.ok) throw new Error(`terminal read failed (${response.status})`);
        const message = await response.json();
        if (sessionId !== session) return;
        if (message.data_base64) {
          await queueTerminalOutput(base64ToBytes(message.data_base64));
        }
        if (message.closed) {
          abandonHttpSession(session, 'Disconnected');
          return;
        }
      } catch (error) {
        abandonHttpSession(session, error.message);
        return;
      }
    }
  }

  async function startHttpFallback(backend) {
    if (sessionId || httpFallbackStarting) return;
    clearTimeout(reconnectTimer);
    clearTimeout(reconnectStableTimer);
    reconnectTimer = undefined;
    reconnectStableTimer = undefined;
    httpFallbackStarting = true;
    httpInputReady = false;
    socket?.close();
    socket = undefined;
    clearOutputFlow();
    discardInputOperations('A paste operation was canceled because WebSocket is unavailable');
    setStatus('Connecting (HTTP fallback)…');
    try {
      const response = await fetchWithTimeout(apiUrl('sessions'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend_id: backend.id, cols: terminal.cols, rows: terminal.rows }),
      });
      if (!response.ok) throw new Error((await response.json()).detail || 'Could not attach');
      const session = await response.json();
      if (!terminal || currentBackend?.id !== backend.id || !httpFallbackStarting) {
        fetch(apiUrl(`sessions/${session.id}`), { method: 'DELETE' }).catch(() => {});
        return;
      }
      sessionId = session.id;
      httpFallbackStarting = false;
      clearTimeout(reconnectStableTimer);
      reconnectStableTimer = setTimeout(() => {
        if (sessionId === session.id) reconnectAttempts = 0;
      }, 30_000);
      setStatus('Connected (HTTP fallback)', 'connected');
      readTerminal(session.id);
    } catch (error) {
      if (currentBackend?.id !== backend.id || !httpFallbackStarting) return;
      httpFallbackStarting = false;
      if (authenticationReloading) return;
      setStatus(error.message, 'disconnected');
      scheduleWebSocketReconnect(backend);
    }
  }

  async function recoverWebSocket(backend, expectedMode) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3_000);
    let httpReachable = false;
    try {
      const response = await fetch(appBasePath(), {
        cache: 'no-store',
        redirect: 'manual',
        signal: controller.signal,
      });
      if (response.type === 'opaqueredirect' || requiresAuthentication(response)) {
        beginAuthenticationReload();
        return;
      }
      httpReachable = true;
    } catch (_) {
      // A network failure still uses the bounded reconnect sequence below.
    } finally {
      clearTimeout(timer);
    }
    if (
      socket || sessionId || httpFallbackStarting || authenticationReloading
      || viewMode !== expectedMode || currentBackend?.id !== backend.id
    ) return;
    if (
      expectedMode === 'full' && httpReachable
      && reconnectAttempts >= FULL_WEBSOCKET_RETRIES_BEFORE_HTTP_FALLBACK
    ) {
      void startHttpFallback(backend);
      return;
    }
    scheduleWebSocketReconnect(backend);
  }

  function scheduleWebSocketReconnect(backend) {
    if (
      reconnectTimer || sessionId || httpFallbackStarting || !terminal
      || selectedBackendId() !== backend.id
    ) return;
    const delay = Math.min(1000 * (2 ** reconnectAttempts), 10_000);
    reconnectAttempts += 1;
    setStatus(`Reconnecting in ${Math.ceil(delay / 1000)}s…`);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined;
      if (viewMode === 'panes') startPaneWebSocket(backend);
      else startWebSocket(backend);
    }, delay);
  }

  function startWebSocket(backend) {
    let opened = false;
    let attached = false;
    const nextSocket = new WebSocket(wsUrl(backend.id));
    const flow = {
      socket: nextSocket,
      attached: false,
      inputReady: false,
      parsedBytes: 0,
      acknowledgedBytes: 0,
      ackTimer: undefined,
    };
    clearOutputFlow();
    outputFlow = flow;
    socket = nextSocket;
    nextSocket.binaryType = 'arraybuffer';
    clearTimeout(connectTimer);
    connectTimer = setTimeout(() => {
      if (socket === nextSocket && !opened) nextSocket.close();
    }, 2000);
    nextSocket.onopen = () => {
      if (socket !== nextSocket) {
        nextSocket.close();
        return;
      }
      opened = true;
      clearTimeout(connectTimer);
      setStatus('Attaching…');
      fitAddon.fit();
      nextSocket.send(JSON.stringify({
        type: 'resize', cols: terminal.cols, rows: terminal.rows, output_ack: true,
      }));
      connectTimer = setTimeout(() => {
        if (socket === nextSocket && !attached) nextSocket.close();
      }, 3000);
    };
    nextSocket.onmessage = (event) => {
      if (socket !== nextSocket) return;
      if (typeof event.data === 'string') {
        const message = JSON.parse(event.data);
        if (message.type === 'attached') {
          attached = true;
          flow.attached = true;
          clearTimeout(connectTimer);
          connectTimer = undefined;
          clearTimeout(reconnectStableTimer);
          reconnectStableTimer = setTimeout(() => {
            if (socket === nextSocket) reconnectAttempts = 0;
          }, 30_000);
          setStatus('Connected', 'connected');
          scheduleInputDrain();
        } else if (message.type === 'ping') {
          nextSocket.send(JSON.stringify({ type: 'pong' }));
        } else if (message.type === 'error') {
          showBrowserToast(message.message);
        }
        return;
      }
      flow.inputReady = true;
      queueTerminalOutput(new Uint8Array(event.data), flow);
      scheduleInputDrain();
    };
    nextSocket.onclose = () => {
      if (socket !== nextSocket) return;
      clearTimeout(connectTimer);
      clearTimeout(reconnectStableTimer);
      connectTimer = undefined;
      reconnectStableTimer = undefined;
      socket = undefined;
      clearOutputFlow(flow);
      if (attached && !sessionId) scheduleWebSocketReconnect(backend);
      else if (!sessionId && !httpFallbackStarting && viewMode === 'full') {
        void recoverWebSocket(backend, 'full');
      }
    };
    nextSocket.onerror = () => {
      // onclose starts the fallback or reconnect sequence.
    };
  }

  function attachFull(backend) {
    clearTimeout(reconnectTimer);
    clearTimeout(reconnectStableTimer);
    clearTimeout(reconnectReloadTimer);
    reconnectTimer = undefined;
    reconnectStableTimer = undefined;
    reconnectReloadTimer = undefined;
    reconnectAttempts = 0;
    receivedFrames = 0;
    clearTimeout(inputDrainTimer);
    inputDrainTimer = undefined;
    clearPendingMouseMotion();
    inputBuffer.clear();
    inputOperations.length = 0;
    httpInputInFlight = false;
    httpInputReady = false;
    currentBackend = backend;
    viewMode = 'full';
    httpFallbackStarting = false;
    picker.hidden = true;
    terminalView.hidden = false;
    terminalView.dataset.view = 'full';
    terminalHost.hidden = false;
    panesView.hidden = true;
    fullModeButton.setAttribute('aria-pressed', 'true');
    panesModeButton.setAttribute('aria-pressed', 'false');
    backendName.textContent = backend.label;
    setStatus('Connecting…');
    terminalHost.replaceChildren();

    terminal = new Terminal({
      cursorBlink: true,
      customGlyphs: true,
      fontFamily: terminalFontFamily,
      fontSize: DEFAULT_TERMINAL_FONT_SIZE,
      logLevel: 'error',
      scrollback: 20_000,
      theme: terminalTheme,
    });
    fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(terminalHost);
    enableWebglRenderer(terminal);
    terminal.parser.registerOscHandler(52, handleOsc52);
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== 'keydown') return true;
      const modifiedEnter = physicalModifiedEnterData(event);
      if (modifiedEnter) {
        // CSI-u keeps modified Enter distinct from plain Enter. For example,
        // Pi uses Shift+Enter for a newline and Alt+Enter for a follow-up.
        event.preventDefault();
        sendInput(modifiedEnter);
        return false;
      }
      if (!event.metaKey) return true;
      const commandKey = { ArrowLeft: '\x01', ArrowRight: '\x05', Backspace: '\x15' }[event.key];
      if (!commandKey) return true;
      // Match standard macOS Terminal/iTerm editing: Cmd+Left/Right send
      // Ctrl-A/Ctrl-E; Cmd+Backspace sends Ctrl-U (kill to line start).
      event.preventDefault();
      sendInput(commandKey);
      return false;
    });
    terminal.element.addEventListener('contextmenu', (event) => event.preventDefault());
    terminal.element.addEventListener('paste', (event) => {
      const image = clipboardImageFromPaste(event);
      if (!image) return;
      event.preventDefault();
      void sendClipboardImage(image).catch(() => showBrowserToast('Could not read clipboard image'));
    }, true);
    fitAddon.fit();
    syncTerminalGridMetrics(terminal);
    terminal.focus();

    terminal.onData(sendInput);
    resizeObserver = new ResizeObserver(sendResize);
    resizeObserver.observe(terminalHost);
    startWebSocket(backend);
    startNavigationPolling();
  }

  function openBackend(backend, updateUrl = true) {
    if (updateUrl) setSelectedBackend(backend.id);
    if (selectedViewMode() === 'panes') void attachPanes(backend);
    else attachFull(backend);
  }

  async function startNamedSession(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const input = form.querySelector('input');
    const button = form.querySelector('button');
    const name = input.value.trim();
    input.value = name;
    if (!form.reportValidity()) return;

    pickerError.hidden = true;
    input.disabled = true;
    button.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const response = await fetchWithTimeout(apiUrl('backends'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      }, 20_000);
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || `session start failed (${response.status})`);
      openBackend(result.backend);
    } catch (error) {
      pickerError.textContent = error.message;
      pickerError.hidden = false;
    } finally {
      input.disabled = false;
      button.disabled = false;
      form.removeAttribute('aria-busy');
    }
  }

  function addSessionRow() {
    const existing = backendList.querySelector('.start-session-row');
    if (existing) {
      existing.querySelector('input').focus();
      return;
    }
    backendList.querySelector('.empty-backends')?.remove();
    const form = document.createElement('form');
    form.className = 'start-session-row';
    form.setAttribute('aria-label', 'Start a named session');
    const input = document.createElement('input');
    input.type = 'text';
    input.name = 'session-name';
    input.maxLength = 64;
    input.pattern = '[A-Za-z0-9._\\-]+';
    input.placeholder = 'Session name';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.required = true;
    input.setAttribute('aria-label', 'Session name');
    const button = document.createElement('button');
    button.type = 'submit';
    button.textContent = 'Start';
    form.append(input, button);
    form.addEventListener('submit', startNamedSession);
    backendList.append(form);
    input.focus();
  }

  async function loadBackends(openSelected = true) {
    backendList.replaceChildren();
    pickerError.hidden = true;
    try {
      const response = await fetchWithTimeout(apiUrl('backends'), { cache: 'no-store' });
      if (!response.ok) throw new Error(`backend discovery failed (${response.status})`);
      const { backends } = await response.json();
      if (!backends.length) {
        const empty = document.createElement('div');
        empty.className = 'empty-backends muted';
        empty.textContent = 'No running Herdr client sockets found.';
        backendList.append(empty);
        return [];
      }
      for (const backend of backends) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'backend';
        button.textContent = backend.label;
        button.addEventListener('click', () => openBackend(backend));
        backendList.append(button);
      }
      const selected = selectedBackendId();
      if (openSelected && selected) {
        const backend = backends.find((candidate) => candidate.id === selected);
        if (backend) openBackend(backend, false);
        else {
          pickerError.textContent = 'The session in this URL is not running on this host.';
          pickerError.hidden = false;
        }
      }
      return backends;
    } catch (error) {
      pickerError.textContent = error.message;
      pickerError.hidden = false;
      return [];
    }
  }

  async function restoreUrlSelection() {
    const selected = selectedBackendId();
    if (!selected) {
      showPicker(false);
      return;
    }
    showPicker(false, false);
    const backends = await loadBackends(false);
    const backend = backends.find((candidate) => candidate.id === selected);
    if (backend) openBackend(backend, false);
    else {
      pickerError.textContent = 'The session in this URL is not running on this host.';
      pickerError.hidden = false;
    }
  }

  document.querySelector('#refresh').addEventListener('click', () => loadBackends(false));
  document.querySelector('#back').addEventListener('click', () => showPicker());
  fullModeButton.addEventListener('click', () => switchViewMode('full'));
  panesModeButton.addEventListener('click', () => switchViewMode('panes'));
  paneBrowse.addEventListener('click', () => openMobileSheet('browse', paneBrowse));
  paneSidebarToggle.addEventListener('pointerdown', (event) => event.preventDefault());
  paneSidebarToggle.addEventListener('click', () => {
    setDesktopPaneSidebarCollapsed(!desktopPaneSidebarCollapsed);
  });
  renderDesktopPaneSidebar();
  for (const button of mobileModifiers.querySelectorAll('[data-modifier]')) {
    button.addEventListener('pointerdown', (event) => event.preventDefault());
    button.addEventListener('click', () => toggleMobileModifier(button.dataset.modifier));
  }
  for (const button of mobileSpecialKeys.querySelectorAll('[data-terminal-key]')) {
    button.addEventListener('pointerdown', (event) => event.preventDefault());
    button.addEventListener('click', () => sendMobileTerminalKey(button.dataset.terminalKey));
  }
  mobileMouseModeButton.addEventListener('pointerdown', (event) => event.preventDefault());
  mobileMouseModeButton.addEventListener('click', () => {
    setMobileMouseMode(!mobileMouseMode);
  });
  mobileKeyboardLockButton.addEventListener('pointerdown', (event) => event.preventDefault());
  mobileKeyboardLockButton.addEventListener('click', () => {
    setMobileKeyboardLocked(!mobileKeyboardLocked);
  });
  mobileNavigationModeButton.addEventListener('pointerdown', (event) => event.preventDefault());
  mobileNavigationModeButton.addEventListener('click', () => {
    setMobileNavigationMode(!mobileNavigationMode);
  });
  mobileSnapshotButton.addEventListener('pointerdown', (event) => event.preventDefault());
  mobileSnapshotButton.addEventListener('click', () => {
    toggleTerminalSnapshot(selectedPaneTerminal());
  });
  renderMobileMouseMode();
  renderMobileKeyboardLock();
  renderMobileNavigationMode();
  renderMobileSnapshotButton(false);
  for (const button of mobileArrows.querySelectorAll('[data-arrow]')) {
    bindRepeatableMobileKey(button, () => sendMobileArrow(button.dataset.arrow));
  }
  for (const button of mobileNavigationRow.querySelectorAll('[data-navigation-key]')) {
    bindRepeatableMobileKey(
      button, () => sendMobileNavigationKey(button.dataset.navigationKey),
    );
  }
  for (const button of mobileToolbar.querySelectorAll('button')) {
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.addEventListener('click', () => openMobileSheet(button.dataset.sheet, button));
  }
  document.querySelector('#sheet-backdrop').addEventListener('click', () => closeMobileSheet());
  sheetSessions.addEventListener('click', () => showPicker());
  document.querySelector('#sheet-close').addEventListener('click', () => closeMobileSheet());
  document.addEventListener('keydown', handleMobileTerminalKeyDown, true);
  document.addEventListener('beforeinput', handleMobileTerminalBeforeInput, true);
  document.addEventListener('selectionchange', handleMobileCaretSelection, true);
  document.addEventListener('select', handleMobileCaretSelection, true);
  document.addEventListener(
    'compositionstart', handleMobilePredictionCompositionStart, true,
  );
  document.addEventListener(
    'compositionupdate', handleMobilePredictionCompositionUpdate, true,
  );
  document.addEventListener(
    'compositionend', handleMobilePredictionCompositionEnd, true,
  );
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && openSheetName) {
      event.preventDefault();
      closeMobileSheet();
    }
  });
  function syncVisualViewportLayout() {
    visualViewportFrame = undefined;
    const root = document.documentElement;
    const viewport = window.visualViewport;
    if (!mobileQuery.matches || !viewport) {
      for (const property of [
        '--visual-viewport-top', '--visual-viewport-left',
        '--visual-viewport-width', '--visual-viewport-height',
      ]) root.style.removeProperty(property);
      delete root.dataset.mobileKeyboard;
      mobileViewportBaselineWidth = undefined;
      mobileViewportMaximumHeight = 0;
      return;
    }
    root.style.setProperty('--visual-viewport-top', `${viewport.offsetTop.toFixed(2)}px`);
    root.style.setProperty('--visual-viewport-left', `${viewport.offsetLeft.toFixed(2)}px`);
    root.style.setProperty('--visual-viewport-width', `${viewport.width.toFixed(2)}px`);
    root.style.setProperty('--visual-viewport-height', `${viewport.height.toFixed(2)}px`);

    if (
      mobileViewportBaselineWidth === undefined
      || Math.abs(viewport.width - mobileViewportBaselineWidth) > 1
    ) {
      mobileViewportBaselineWidth = viewport.width;
      mobileViewportMaximumHeight = viewport.height;
    } else {
      mobileViewportMaximumHeight = Math.max(
        mobileViewportMaximumHeight, viewport.height,
      );
    }
    const minimumShrink = Math.max(
      MOBILE_KEYBOARD_MINIMUM_SHRINK_PX, mobileViewportMaximumHeight * 0.2,
    );
    const keyboardOpen = viewport.scale === 1
      && mobileViewportMaximumHeight - viewport.height >= minimumShrink;
    root.dataset.mobileKeyboard = keyboardOpen ? 'open' : 'closed';
  }

  function scheduleVisualViewportLayout() {
    if (visualViewportFrame !== undefined) return;
    visualViewportFrame = requestAnimationFrame(syncVisualViewportLayout);
  }

  function handleResponsiveLayout() {
    scheduleVisualViewportLayout();
    if (mobileQuery.matches && viewMode === 'full' && currentBackend) {
      switchViewMode('panes');
      return;
    }
    if (!mobileQuery.matches) closeMobileSheet(false);
    startNavigationPolling();
    if (viewMode === 'panes' && paneCompact !== mobileQuery.matches) rebuildPaneView();
    else sendResize();
  }
  mobileQuery.addEventListener('change', handleResponsiveLayout);
  window.visualViewport?.addEventListener('resize', scheduleVisualViewportLayout);
  window.visualViewport?.addEventListener('scroll', scheduleVisualViewportLayout);
  scheduleVisualViewportLayout();
  window.addEventListener('resize', () => {
    clearTimeout(paneResponsiveTimer);
    paneResponsiveTimer = setTimeout(handleResponsiveLayout, 100);
  });
  addSessionButton.addEventListener('click', addSessionRow);
  telemetry.addEventListener('click', attemptReconnect);
  window.addEventListener('online', attemptReconnect);
  window.addEventListener('pagehide', () => {
    stopMobileKeyRepeat();
    socket?.close();
    if (sessionId) {
      fetch(apiUrl(`sessions/${sessionId}`), { method: 'DELETE', keepalive: true }).catch(() => {});
    }
  });
  window.addEventListener('popstate', restoreUrlSelection);
  appearanceQuery.addEventListener('change', syncHerdrTheme);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      void syncHerdrTheme();
      void refreshNavigation();
    }
  });
  window.setInterval(() => {
    if (!document.hidden) void syncHerdrTheme();
  }, 30_000);
  startTransportRateMeter();
  restoreUrlSelection();
})().catch((error) => {
  const pickerError = document.querySelector('#picker-error');
  pickerError.textContent = `Could not start xterm.js: ${error.message}`;
  pickerError.hidden = false;
});
