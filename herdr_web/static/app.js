import { InputByteBuffer, isDisposableMouseMotion } from './input-buffer.js';
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
  const backendName = document.querySelector('#backend-name');
  const mobileToolbar = document.querySelector('#mobile-toolbar');
  const mobileSheet = document.querySelector('#mobile-sheet');
  const sheetTitle = document.querySelector('#sheet-title');
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
  let httpInputInFlight = false;
  let httpInputReady = false;
  const inputEncoder = new TextEncoder();
  const inputBuffer = new InputByteBuffer(inputEncoder);
  const inputOperations = [];
  const INPUT_BATCH_BYTES = 16 * 1024;
  const INPUT_WEBSOCKET_HIGH_WATER_BYTES = 32 * 1024;
  const OUTPUT_ACK_BATCH_BYTES = 64 * 1024;
  const OUTPUT_ACK_DELAY_MS = 10;
  const MOUSE_MOTION_INTERVAL_MS = 16;
  const MAX_CLIPBOARD_IMAGE_BYTES = 16 * 1024 * 1024;
  const clipboardImageExtensions = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/bmp': 'bmp',
  };
  const appearanceQuery = matchMedia('(prefers-color-scheme: light)');
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
    terminalTheme = xtermTheme(palette);
    appliedThemeFingerprint = fingerprint;
    if (terminal) terminal.options.theme = terminalTheme;
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

  function setStatus(message, state = 'connecting') {
    status.textContent = message;
    connectionIndicator.dataset.state = state;
    telemetry.dataset.state = state;
    telemetry.title = state === 'disconnected' ? 'Disconnected. Click to reconnect.' : '';
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
    startWebSocket(currentBackend);
  }

  function startTransportRateMeter() {
    window.setInterval(() => {
      // Count terminal update messages, not browser canvas repaints.
      fps.textContent = `${receivedFrames} FPS`;
      receivedFrames = 0;
    }, 1000);
  }

  function showBrowserToast(message) {
    const toast = document.createElement('div');
    toast.className = 'web-toast';
    toast.textContent = message;
    toastHost.append(toast);
    window.setTimeout(() => toast.remove(), 5000);
  }

  async function writeBrowserClipboard(text) {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard API is unavailable');
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

  async function sendClipboardImage(image) {
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
      offset: inputBuffer.enqueuedBytes,
      extension: image.extension,
      ready: false,
    };
    inputOperations.push(operation);
    scheduleInputDrain();
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

  function agentStatus(record) {
    const badge = document.createElement('span');
    badge.className = 'agent-status';
    badge.dataset.status = record.agent_status || 'unknown';
    badge.textContent = record.agent_status || 'unknown';
    return badge;
  }

  function sheetItem(label, detail, current, statusValue, action) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'sheet-item';
    if (current) button.setAttribute('aria-current', 'true');
    const name = document.createElement('span');
    name.className = 'sheet-item-label';
    name.textContent = label;
    const description = document.createElement('span');
    description.className = 'sheet-item-detail';
    description.textContent = detail;
    button.append(name, description);
    if (statusValue) button.append(agentStatus({ agent_status: statusValue }));
    button.addEventListener('click', action);
    return button;
  }

  function closeMobileSheet(refocus = true) {
    openSheetName = undefined;
    mobileSheet.hidden = true;
    for (const button of mobileToolbar.querySelectorAll('button')) {
      button.setAttribute('aria-expanded', 'false');
    }
    if (refocus) terminal?.focus();
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
      spaces: 'Spaces', tabs: 'Tabs', agents: 'Agents', more: 'More',
    }[openSheetName];

    if (openSheetName === 'more') {
      const reconnect = document.createElement('button');
      reconnect.type = 'button';
      reconnect.className = 'more-action';
      reconnect.textContent = 'Reconnect terminal';
      reconnect.addEventListener('click', () => {
        closeMobileSheet(false);
        setStatus('Reconnect requested', 'disconnected');
        attemptReconnect();
      });
      const sessionsButton = document.createElement('button');
      sessionsButton.type = 'button';
      sessionsButton.className = 'more-action';
      sessionsButton.textContent = 'Back to sessions';
      sessionsButton.addEventListener('click', () => {
        closeMobileSheet(false);
        showPicker();
      });
      sheetContent.append(reconnect, sessionsButton);
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
    if (!currentBackend || !mobileQuery.matches || document.hidden) return;
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
      navigationSnapshot = result;
      navigationFingerprint = fingerprint;
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
    if (!mobileQuery.matches || !currentBackend) return;
    void refreshNavigation();
    navigationTimer = setInterval(refreshNavigation, 2_000);
  }

  function openMobileSheet(name, trigger) {
    openSheetName = name;
    mobileSheet.hidden = false;
    for (const button of mobileToolbar.querySelectorAll('button')) {
      button.setAttribute('aria-expanded', button === trigger ? 'true' : 'false');
    }
    renderMobileSheet();
    if (name !== 'more') void refreshNavigation();
    document.querySelector('#sheet-close').focus();
  }

  async function fetchWithTimeout(url, options = {}, timeout = 10_000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      return await fetch(url, { ...options, signal: controller.signal });
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

  function queueTerminalOutput(bytes, flow) {
    receivedFrames += 1;
    const activeTerminal = terminal;
    if (!activeTerminal) return;
    // Use xterm's supported write queue. It limits parser work per browser
    // task and gives the renderer an opportunity to paint between batches.
    activeTerminal.write(bytes, () => {
      if (terminal !== activeTerminal) return;
      if (flow) noteParsedOutput(flow, bytes.length);
      else {
        httpInputReady = true;
        scheduleInputDrain();
      }
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
      activeSocket.send(JSON.stringify({
        type: 'clipboard-image',
        extension: operation.extension,
        size: operation.bytes.byteLength,
      }));
      activeSocket.send(operation.bytes);
    }

    if (inputBuffer.length || inputOperations.length) scheduleInputDrain(8);
  }

  function abandonHttpSession(session, message) {
    if (sessionId !== session) return;
    sessionId = undefined;
    httpInputReady = false;
    setStatus(message, 'disconnected');
    fetch(apiUrl(`sessions/${session}`), { method: 'DELETE' }).catch(() => {});
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
    inputBuffer.append(pendingMouseMotion);
    pendingMouseMotion = undefined;
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

  function showPicker(updateUrl = true, refresh = true) {
    closeMobileSheet(false);
    clearInterval(navigationTimer);
    navigationTimer = undefined;
    navigationSnapshot = undefined;
    navigationFingerprint = undefined;
    navigationRequest = undefined;
    clearTimeout(connectTimer);
    clearTimeout(reconnectTimer);
    clearTimeout(reconnectStableTimer);
    clearTimeout(reconnectReloadTimer);
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
    terminal?.dispose();
    terminal = undefined;
    fitAddon = undefined;
    webglAddon = undefined;
    terminalHost.replaceChildren();
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
    if (!terminal || !fitAddon || resizeQueued) return;
    const activeTerminal = terminal;
    const activeFitAddon = fitAddon;
    resizeQueued = true;
    requestAnimationFrame(() => {
      resizeQueued = false;
      if (terminal !== activeTerminal || fitAddon !== activeFitAddon) return;
      activeFitAddon.fit();
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
        if (message.data_base64) queueTerminalOutput(base64ToBytes(message.data_base64));
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
    discardInputOperations('Image paste was canceled because WebSocket is unavailable');
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
      setStatus('Connected (HTTP fallback)', 'connected');
      readTerminal(session.id);
    } catch (error) {
      if (currentBackend?.id !== backend.id || !httpFallbackStarting) return;
      httpFallbackStarting = false;
      setStatus(error.message, 'disconnected');
    }
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
      startWebSocket(backend);
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
      if (socket === nextSocket && !opened) startHttpFallback(backend);
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
        if (socket === nextSocket && !attached) startHttpFallback(backend);
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
      if (!opened && !sessionId && !httpFallbackStarting) startHttpFallback(backend);
      else if ((opened || attached) && !sessionId) scheduleWebSocketReconnect(backend);
    };
    nextSocket.onerror = () => {
      // onclose starts the fallback or reconnect sequence.
    };
  }

  function attach(backend) {
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
    httpFallbackStarting = false;
    picker.hidden = true;
    terminalView.hidden = false;
    backendName.textContent = backend.label;
    setStatus('Connecting…');
    terminalHost.replaceChildren();

    terminal = new Terminal({
      cursorBlink: true,
      customGlyphs: true,
      fontFamily: terminalFontFamily,
      fontSize: 14,
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
      if (event.key === 'Enter' && event.shiftKey && !event.metaKey && !event.ctrlKey && !event.altKey) {
        // Pi requires the CSI-u Shift+Enter sequence (ESC [ 13 ; 2 u) to
        // distinguish a newline from plain Enter, which submits the prompt.
        event.preventDefault();
        sendInput('\x1b[13;2u');
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
    terminal.focus();

    terminal.onData(sendInput);
    resizeObserver = new ResizeObserver(sendResize);
    resizeObserver.observe(terminalHost);
    startWebSocket(backend);
    startNavigationPolling();
  }

  function openBackend(backend, updateUrl = true) {
    if (updateUrl) setSelectedBackend(backend.id);
    attach(backend);
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
  for (const button of mobileToolbar.querySelectorAll('button')) {
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.addEventListener('click', () => openMobileSheet(button.dataset.sheet, button));
  }
  document.querySelector('#sheet-backdrop').addEventListener('click', () => closeMobileSheet());
  document.querySelector('#sheet-close').addEventListener('click', () => closeMobileSheet());
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && openSheetName) {
      event.preventDefault();
      closeMobileSheet();
    }
  });
  mobileQuery.addEventListener('change', () => {
    if (!mobileQuery.matches) closeMobileSheet(false);
    startNavigationPolling();
    sendResize();
  });
  addSessionButton.addEventListener('click', addSessionRow);
  telemetry.addEventListener('click', attemptReconnect);
  window.addEventListener('online', attemptReconnect);
  window.addEventListener('pagehide', () => {
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
  }, 2_000);
  startTransportRateMeter();
  restoreUrlSelection();
})().catch((error) => {
  const pickerError = document.querySelector('#picker-error');
  pickerError.textContent = `Could not start xterm.js: ${error.message}`;
  pickerError.hidden = false;
});
