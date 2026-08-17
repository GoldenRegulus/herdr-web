(() => {
  const picker = document.querySelector('#picker');
  const terminalView = document.querySelector('#terminal-view');
  const backendList = document.querySelector('#backends');
  const pickerError = document.querySelector('#picker-error');
  const status = document.querySelector('#connection-status');
  const connectionIndicator = document.querySelector('#connection-indicator');
  const fps = document.querySelector('#fps');
  const telemetry = document.querySelector('#telemetry');
  const terminalProgress = document.querySelector('#terminal-progress');
  const progressValue = document.querySelector('#progress-value');
  const progressText = document.querySelector('#progress-text');
  const toastHost = document.querySelector('#web-toasts');
  const terminalHost = document.querySelector('#terminal');
  const backendName = document.querySelector('#backend-name');

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
  let resizeObserver;
  let resizeQueued = false;
  let outputQueued = [];
  let outputAnimationFrame;
  let receivedFrames = 0;
  const MAX_CLIPBOARD_IMAGE_BYTES = 16 * 1024 * 1024;
  const clipboardImageExtensions = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/bmp': 'bmp',
  };

  function setStatus(message, state = 'connecting') {
    status.textContent = message;
    connectionIndicator.dataset.state = state;
    telemetry.dataset.state = state;
    telemetry.title = state === 'disconnected' ? 'Disconnected. Click to reconnect.' : '';
    if (state !== 'disconnected') {
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
      // Count bridge frames, not browser repaint frames. The server suppresses
      // unchanged Herdr frames and the bridge caps changed-frame delivery at
      // 60 Hz, so this is the actual screen-update transport rate.
      fps.textContent = `${receivedFrames} FPS`;
      receivedFrames = 0;
    }, 1000);
  }

  async function requestBrowserNotificationPermission() {
    if (!('Notification' in window) || Notification.permission !== 'default') return;
    try {
      await Notification.requestPermission();
    } catch (_) {
      // In-app toasts remain available when the browser denies this API.
    }
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

  function showBrowserNotification(message) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const [title, body] = message.split(/: (.+)/, 2);
    try {
      new Notification(title || 'Herdr', { body: body || '' });
    } catch (_) {
      // Some embedded browsers expose Notification but reject construction.
    }
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
    if (socket?.readyState !== WebSocket.OPEN) {
      showBrowserToast('Image paste needs a WebSocket connection');
      return;
    }
    if (image.file.size <= 0 || image.file.size > MAX_CLIPBOARD_IMAGE_BYTES) {
      showBrowserToast('Image must be smaller than 16 MiB');
      return;
    }
    const bytes = await image.file.arrayBuffer();
    socket.send(JSON.stringify({
      type: 'clipboard-image', extension: image.extension, size: bytes.byteLength,
    }));
    socket.send(bytes);
  }

  function handleOsc9(message) {
    // OSC 9;4;<state>;<percent> is the Windows Terminal progress convention.
    // Herdr's terminal toast is OSC 9;<message>, so it remains separate.
    const progress = /^4;([0-4]);(?:([0-9]{1,3}))?$/.exec(message);
    if (!progress) {
      showBrowserToast(message);
      showBrowserNotification(message);
      return true;
    }
    const state = Number(progress[1]);
    if (state === 0) {
      terminalProgress.hidden = true;
      return true;
    }
    const names = ['clear', 'normal', 'error', 'paused', 'indeterminate'];
    terminalProgress.hidden = false;
    terminalProgress.dataset.state = names[state];
    if (state === 4) {
      progressValue.removeAttribute('value');
      progressText.textContent = 'Working';
    } else {
      const value = Math.min(100, Number(progress[2] || 0));
      progressValue.value = value;
      progressText.textContent = `${value}%`;
    }
    return true;
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

  function wsUrl(backendId) {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${location.host}${appBasePath().replace(/\/$/, '')}/ws/${encodeURIComponent(backendId)}`;
  }

  function queueTerminalOutput(bytes) {
    receivedFrames += 1;
    outputQueued.push(bytes);
    if (outputAnimationFrame) return;
    // Coalesce network bursts at the display refresh boundary. xterm still
    // receives every byte in order, while the browser renders at most once per
    // animation frame (normally 60 Hz).
    outputAnimationFrame = requestAnimationFrame(() => {
      outputAnimationFrame = undefined;
      const total = outputQueued.reduce((size, chunk) => size + chunk.length, 0);
      const joined = new Uint8Array(total);
      let offset = 0;
      for (const chunk of outputQueued) {
        joined.set(chunk, offset);
        offset += chunk.length;
      }
      outputQueued = [];
      terminal?.write(joined);
    });
  }

  function showPicker(updateUrl = true, refresh = true) {
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
    httpFallbackStarting = false;
    if (sessionId) fetch(apiUrl(`sessions/${sessionId}`), { method: 'DELETE' });
    sessionId = undefined;
    currentBackend = undefined;
    resizeObserver?.disconnect();
    resizeObserver = undefined;
    terminal?.dispose();
    terminal = undefined;
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
    if (!terminal || resizeQueued) return;
    resizeQueued = true;
    requestAnimationFrame(() => {
      resizeQueued = false;
      fitAddon.fit();
      const resize = { type: 'resize', cols: terminal.cols, rows: terminal.rows };
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(resize));
      } else if (sessionId) {
        fetch(apiUrl(`sessions/${sessionId}/resize`), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(resize),
        }).catch(() => { setStatus('Disconnected', 'disconnected'); });
      }
    });
  }

  function sendInput(data) {
    const bytes = new TextEncoder().encode(data);
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(bytes);
      return;
    }
    if (!sessionId) return;
    fetch(apiUrl(`sessions/${sessionId}/input`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data_base64: bytesToBase64(bytes) }),
    }).catch(() => { setStatus('Disconnected', 'disconnected'); });
  }

  async function readTerminal(session) {
    while (sessionId === session) {
      try {
        const response = await fetch(apiUrl(`sessions/${session}/read`), { cache: 'no-store' });
        if (!response.ok) throw new Error(`terminal read failed (${response.status})`);
        const message = await response.json();
        if (message.data_base64) queueTerminalOutput(base64ToBytes(message.data_base64));
        if (message.closed) {
          setStatus('Disconnected', 'disconnected');
          sessionId = undefined;
          return;
        }
      } catch (error) {
        setStatus(error.message, 'disconnected');
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
    socket?.close();
    socket = undefined;
    setStatus('Connecting (HTTP fallback)…');
    try {
      const response = await fetch(apiUrl('sessions'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend_id: backend.id, cols: terminal.cols, rows: terminal.rows }),
      });
      if (!response.ok) throw new Error((await response.json()).detail || 'Could not attach');
      const session = await response.json();
      sessionId = session.id;
      setStatus('Connected (HTTP fallback)', 'connected');
      readTerminal(session.id);
    } catch (error) {
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
    const nextSocket = new WebSocket(wsUrl(backend.id));
    socket = nextSocket;
    nextSocket.binaryType = 'arraybuffer';
    connectTimer = setTimeout(() => {
      if (!opened) startHttpFallback(backend);
    }, 2000);
    nextSocket.onopen = () => {
      if (socket !== nextSocket) {
        nextSocket.close();
        return;
      }
      opened = true;
      clearTimeout(connectTimer);
      clearTimeout(reconnectStableTimer);
      reconnectStableTimer = setTimeout(() => {
        if (socket === nextSocket) reconnectAttempts = 0;
      }, 30_000);
      setStatus('Connected', 'connected');
      sendResize();
    };
    nextSocket.onmessage = (event) => {
      if (typeof event.data === 'string') {
        const message = JSON.parse(event.data);
        if (message.type === 'error') showBrowserToast(message.message);
        return;
      }
      queueTerminalOutput(new Uint8Array(event.data));
    };
    nextSocket.onclose = () => {
      if (socket !== nextSocket) return;
      clearTimeout(connectTimer);
      clearTimeout(reconnectStableTimer);
      reconnectStableTimer = undefined;
      socket = undefined;
      if (!opened && !sessionId && !httpFallbackStarting) startHttpFallback(backend);
      else if (opened && !sessionId) scheduleWebSocketReconnect(backend);
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
    currentBackend = backend;
    httpFallbackStarting = false;
    terminalProgress.hidden = true;
    picker.hidden = true;
    terminalView.hidden = false;
    backendName.textContent = backend.label;
    setStatus('Connecting…');
    terminalHost.replaceChildren();

    terminal = new Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 14,
      scrollback: 20_000,
      theme: { background: '#101216', foreground: '#d8dee9', cursor: '#eceff4' },
      allowProposedApi: false,
    });
    fitAddon = new FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(terminalHost);
    // The bridge asks Herdr's terminal-notification path to emit OSC 9.
    // xterm hands us its sanitized payload without rendering control bytes.
    terminal.parser.registerOscHandler(9, handleOsc9);
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
    // Keep right-click available for Herdr/xterm mouse handling instead of
    // opening the browser context menu over the terminal.
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
  }

  function openBackend(backend, updateUrl = true) {
    if (updateUrl) {
      setSelectedBackend(backend.id);
      // This runs from the session-picker click, which is a browser-recognized
      // user gesture and therefore may request Chrome notification permission.
      void requestBrowserNotificationPermission();
    }
    attach(backend);
  }

  async function loadBackends(openSelected = true) {
    backendList.replaceChildren();
    pickerError.hidden = true;
    try {
      const response = await fetch(apiUrl('backends'), { cache: 'no-store' });
      if (!response.ok) throw new Error(`backend discovery failed (${response.status})`);
      const { backends } = await response.json();
      if (!backends.length) {
        backendList.textContent = 'No running Herdr client sockets found.';
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
  telemetry.addEventListener('click', attemptReconnect);
  window.addEventListener('popstate', restoreUrlSelection);
  startTransportRateMeter();
  restoreUrlSelection();
})();
