const parameters = new URLSearchParams(location.search);
const backend = parameters.get('backend');
const pane = parameters.get('pane');
let piSessionId = parameters.get('session');
const messages = document.querySelector('#messages');
const detail = document.querySelector('#session-detail');
const liveState = document.querySelector('#live-state');
const sessionSize = document.querySelector('#session-size');
const themeToggle = document.querySelector('#theme-toggle');
const toggleAllMessages = document.querySelector('#toggle-all-messages');
const template = document.querySelector('#message-template');
const messageNavigation = document.querySelector('#message-navigation');
const previousMessage = document.querySelector('#previous-message');
const nextMessage = document.querySelector('#next-message');
const messagePosition = document.querySelector('#message-position');
const messageToc = document.querySelector('#message-toc');
const messageTocItems = document.querySelector('#message-toc-items');
const tocToggle = document.querySelector('#toc-toggle');
const tocClose = document.querySelector('#toc-close');
const filesOpen = document.querySelector('#files-open');
const filesDialog = document.querySelector('#files-dialog');
const filesClose = document.querySelector('#files-close');
const filesSearch = document.querySelector('#files-search');
const filesList = document.querySelector('#files-list');
const filesStatus = document.querySelector('#files-status');
const scratchpadOpen = document.querySelector('#scratchpad-open');
const scratchpadDialog = document.querySelector('#scratchpad-dialog');
const scratchpadClose = document.querySelector('#scratchpad-close');
const scratchpadEditor = document.querySelector('#scratchpad-editor');
const scratchpadPreview = document.querySelector('#scratchpad-preview');
const scratchpadStatus = document.querySelector('#scratchpad-status');
const scratchpadToolbar = document.querySelector('#scratchpad-toolbar');
const mobileToc = matchMedia('(max-width: 700px)');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let fingerprint;
let resolvedPane = pane;
let currentMessageIndex = 0;
let refreshDelay = 3_000;
let refreshTimer;
let scratchpadLoaded = false;
let scratchpadSaveTimer;
let scratchpadRevision = 0;
let markdownFiles;
const expandedFileDirectories = new Set();
let fileTreeStateLoadedFor;
const collapsedMessageIds = new Set();
const expandedMessageIds = new Set();
const rawMessageIds = new Set();
let allMessagesCollapsed = true;
let mermaidRenderSequence = 0;
const THEME_KEY = 'herdr-web-preview-theme';
const systemLightTheme = matchMedia('(prefers-color-scheme: light)');

function markdownFilesUrl(fileId) {
  if (!piSessionId) return undefined;
  const base = `${window.herdrWebBasePath}api/pi-sessions/${encodeURIComponent(piSessionId)}/markdown-files`;
  return fileId ? `${base}/${encodeURIComponent(fileId)}` : base;
}

function openMarkdownFile(fileId) {
  if (!piSessionId) return;
  const query = new URLSearchParams({ session: piSessionId, file: fileId });
  window.open(`${window.herdrWebBasePath}session-preview?${query}`, '_blank', 'noopener');
}

function fileTreeStorageKey() {
  return piSessionId ? `herdr-web-file-tree:${piSessionId}` : undefined;
}

function loadFileTreeState() {
  const key = fileTreeStorageKey();
  if (!key || fileTreeStateLoadedFor === key) return;
  expandedFileDirectories.clear();
  try {
    const saved = JSON.parse(localStorage.getItem(key) || '[]');
    if (Array.isArray(saved)) saved.forEach((path) => {
      if (typeof path === 'string') expandedFileDirectories.add(path);
    });
  } catch (_) {
    // A corrupt preference should not prevent browsing project files.
  }
  fileTreeStateLoadedFor = key;
}

function saveFileTreeState() {
  const key = fileTreeStorageKey();
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify([...expandedFileDirectories]));
  } catch (_) {
    // Expansion still works for this tab when storage is unavailable.
  }
}

function markdownFileTree(files) {
  const root = { directories: new Map(), files: [] };
  files.forEach((file) => {
    let node = root;
    const parts = file.location ? file.location.split('/') : [];
    parts.forEach((part) => {
      if (!node.directories.has(part)) {
        node.directories.set(part, { directories: new Map(), files: [] });
      }
      node = node.directories.get(part);
    });
    node.files.push(file);
  });
  return root;
}

function fileTreeFile(file) {
  const item = document.createElement('li');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'files-item files-file';
  const name = document.createElement('strong');
  name.textContent = file.name;
  const size = document.createElement('span');
  size.textContent = formatBytes(file.bytes);
  const icon = document.createElement('span');
  icon.textContent = '↗';
  icon.setAttribute('aria-hidden', 'true');
  button.append(name, size, icon);
  button.addEventListener('click', () => openMarkdownFile(file.id));
  item.append(button);
  return item;
}

function fileTreeCount(node) {
  return node.files.length + [...node.directories.values()].reduce(
    (total, child) => total + fileTreeCount(child), 0,
  );
}

function fileTreeDirectory(name, node, parentPath, query) {
  const path = parentPath ? `${parentPath}/${name}` : name;
  const item = document.createElement('li');
  item.className = 'files-directory';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'files-directory-toggle';
  const queryExpanded = Boolean(query);
  const expanded = queryExpanded || expandedFileDirectories.has(path);
  button.setAttribute('aria-expanded', String(expanded));
  const chevron = document.createElement('span');
  chevron.className = 'files-tree-chevron';
  chevron.textContent = '›';
  const label = document.createElement('strong');
  label.textContent = name;
  const count = document.createElement('span');
  const descendantCount = fileTreeCount(node);
  count.textContent = `${descendantCount} ${descendantCount === 1 ? 'file' : 'files'}`;
  button.append(chevron, label, count);
  button.addEventListener('click', () => {
    if (expandedFileDirectories.has(path)) expandedFileDirectories.delete(path);
    else expandedFileDirectories.add(path);
    saveFileTreeState();
    renderMarkdownFiles();
  });
  item.append(button);
  if (expanded) item.append(fileTreeList(node, path, query));
  return item;
}

function fileTreeList(node, parentPath = '', query = '') {
  const list = document.createElement('ul');
  list.className = 'files-tree';
  [...node.directories.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .forEach(([name, child]) => list.append(fileTreeDirectory(name, child, parentPath, query)));
  [...node.files]
    .sort((left, right) => left.name.localeCompare(right.name))
    .forEach((file) => list.append(fileTreeFile(file)));
  return list;
}

function renderMarkdownFiles() {
  loadFileTreeState();
  const query = filesSearch.value.trim().toLocaleLowerCase();
  const visible = (markdownFiles || []).filter((file) => {
    const label = file.location ? `${file.location}/${file.name}` : file.name;
    return !query || label.toLocaleLowerCase().includes(query);
  });
  filesList.replaceChildren();
  if (visible.length) filesList.append(fileTreeList(markdownFileTree(visible), '', query));
  filesStatus.textContent = `${visible.length} of ${(markdownFiles || []).length} files`;
  if (!visible.length) {
    const empty = document.createElement('p');
    empty.textContent = markdownFiles?.length ? 'No matching Markdown files.' : 'No Markdown files found.';
    filesList.append(empty);
  }
}

async function loadMarkdownFiles() {
  const url = markdownFilesUrl();
  if (!url) throw new Error('Waiting for session…');
  filesStatus.textContent = 'Loading…';
  const response = await fetch(url, { cache: 'no-store' });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `Load failed (${response.status})`);
  markdownFiles = data.files || [];
  renderMarkdownFiles();
}

async function openFiles() {
  filesDialog.showModal();
  filesSearch.focus();
  if (!markdownFiles) {
    try {
      await loadMarkdownFiles();
    } catch (error) {
      filesStatus.textContent = error.message;
    }
  }
}

function closeFiles() {
  filesDialog.close();
}

function scratchpadUrl() {
  if (!piSessionId) return undefined;
  return `${window.herdrWebBasePath}api/pi-sessions/${encodeURIComponent(piSessionId)}/scratchpad`;
}

function renderScratchpad() {
  scratchpadPreview.replaceChildren();
  renderMarkdown(scratchpadPreview, scratchpadEditor.value);
}

async function loadScratchpad() {
  const url = scratchpadUrl();
  if (!url) {
    scratchpadStatus.textContent = 'Waiting for session…';
    return;
  }
  scratchpadStatus.textContent = 'Loading…';
  const response = await fetch(url, { cache: 'no-store' });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `Load failed (${response.status})`);
  scratchpadEditor.value = data.markdown || '';
  scratchpadLoaded = true;
  scratchpadStatus.textContent = 'Saved';
  renderScratchpad();
}

async function saveScratchpad(revision) {
  const url = scratchpadUrl();
  if (!url || !scratchpadLoaded) return;
  scratchpadStatus.textContent = 'Saving…';
  try {
    const response = await fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ markdown: scratchpadEditor.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `Save failed (${response.status})`);
    if (revision === scratchpadRevision) scratchpadStatus.textContent = 'Saved';
  } catch (error) {
    scratchpadStatus.textContent = error.message;
  }
}

function queueScratchpadSave() {
  scratchpadRevision += 1;
  const revision = scratchpadRevision;
  clearTimeout(scratchpadSaveTimer);
  scratchpadStatus.textContent = 'Unsaved';
  scratchpadSaveTimer = setTimeout(() => void saveScratchpad(revision), 500);
}

function applyScratchpadFormatting(button) {
  const start = scratchpadEditor.selectionStart;
  const end = scratchpadEditor.selectionEnd;
  const value = scratchpadEditor.value;
  if (button.dataset.line) {
    const lineStart = value.lastIndexOf('\n', start - 1) + 1;
    scratchpadEditor.setRangeText(button.dataset.line, lineStart, lineStart, 'end');
  } else {
    const before = button.dataset.before || '';
    const after = button.dataset.after || '';
    const selected = value.slice(start, end);
    scratchpadEditor.setRangeText(`${before}${selected}${after}`, start, end, 'select');
    scratchpadEditor.setSelectionRange(start + before.length, end + before.length);
  }
  scratchpadEditor.focus();
  renderScratchpad();
  queueScratchpadSave();
}

async function openScratchpad() {
  scratchpadDialog.showModal();
  if (!scratchpadLoaded) {
    try {
      await loadScratchpad();
    } catch (error) {
      scratchpadStatus.textContent = error.message;
    }
  }
  scratchpadEditor.focus();
}

function closeScratchpad() {
  scratchpadDialog.close();
}

function preferredTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  return saved === 'light' || saved === 'dark'
    ? saved : systemLightTheme.matches ? 'light' : 'dark';
}

function mermaidTheme() {
  return document.documentElement.dataset.previewTheme === 'light' ? 'default' : 'dark';
}

async function renderMermaid(host, source) {
  host.dataset.source = source;
  if (!globalThis.mermaid) {
    host.classList.add('mermaid-error');
    host.textContent = source;
    host.title = 'Mermaid did not load';
    return;
  }
  const renderId = `herdr-mermaid-${Date.now()}-${mermaidRenderSequence += 1}`;
  try {
    globalThis.mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      suppressErrorRendering: true,
      theme: mermaidTheme(),
    });
    const { svg } = await globalThis.mermaid.render(renderId, source);
    if (!host.isConnected || host.dataset.source !== source) return;
    const image = document.createElement('img');
    image.alt = 'Mermaid diagram';
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    host.replaceChildren(image);
    host.classList.remove('mermaid-error');
  } catch (error) {
    if (!host.isConnected || host.dataset.source !== source) return;
    host.classList.add('mermaid-error');
    host.textContent = source;
    host.title = `Could not render Mermaid diagram: ${error.message}`;
  }
}

function applyTheme(theme, persist = false) {
  document.documentElement.dataset.previewTheme = theme;
  document.documentElement.style.colorScheme = theme;
  themeToggle.textContent = theme === 'dark' ? 'Light' : 'Dark';
  themeToggle.title = `Use ${theme === 'dark' ? 'light' : 'dark'} theme`;
  themeToggle.setAttribute('aria-pressed', String(theme === 'light'));
  document.querySelector('meta[name="theme-color"]').content = theme === 'light' ? '#eff1f5' : '#181825';
  if (persist) localStorage.setItem(THEME_KEY, theme);
  document.querySelectorAll('.mermaid-diagram').forEach((host) => {
    if (host.dataset.source) void renderMermaid(host, host.dataset.source);
  });
}

applyTheme(preferredTheme());

function apiUrl() {
  const fileId = parameters.get('file');
  if (fileId && piSessionId) return markdownFilesUrl(fileId);
  const query = new URLSearchParams({ limit: '20' });
  if (piSessionId) {
    return `${window.herdrWebBasePath}api/pi-sessions/${encodeURIComponent(piSessionId)}/messages?${query}`;
  }
  if (resolvedPane) query.set('pane_id', resolvedPane);
  return `${window.herdrWebBasePath}api/backends/${encodeURIComponent(backend)}/pi-messages?${query}`;
}

function pinPiSession(sessionId) {
  if (!sessionId || piSessionId === sessionId) return;
  piSessionId = sessionId;
  const url = new URL(location.href);
  url.search = new URLSearchParams({ session: sessionId }).toString();
  history.replaceState(null, '', url);
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value < 1024) return `${value} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let size = value;
  let unit = -1;
  do {
    size /= 1024;
    unit += 1;
  } while (size >= 1024 && unit < units.length - 1);
  return `${size.toFixed(size >= 100 ? 0 : size >= 10 ? 1 : 2)} ${units[unit]}`;
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function appendMath(parent, expression, displayMode, source) {
  const math = document.createElement(displayMode ? 'div' : 'span');
  math.className = displayMode ? 'math-display' : 'math-inline';
  try {
    if (!globalThis.katex) throw new Error('KaTeX did not load');
    globalThis.katex.render(expression, math, {
      displayMode,
      output: 'htmlAndMathml',
      strict: 'warn',
      throwOnError: true,
      trust: false,
    });
  } catch (error) {
    math.classList.add('math-error');
    math.textContent = source;
    math.title = `Could not render formula: ${error.message}`;
  }
  parent.append(math);
}

function appendExternalLink(parent, href, label = href) {
  const link = document.createElement('a');
  link.textContent = label;
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  parent.append(link);
}

// Deliberately render Markdown as DOM nodes rather than injecting model HTML.
function appendInline(parent, text) {
  const pattern = /(?:`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\((https?:\/\/[^\s)]+)\)|\\\((.+?)\\\)|(?<!\\)\$([^$\n]+?)(?<!\\)\$|https?:\/\/[^\s<>"'`]+)/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      const code = document.createElement('code');
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (token.startsWith('**')) {
      const strong = document.createElement('strong');
      strong.textContent = token.slice(2, -2);
      parent.append(strong);
    } else if (token.startsWith('[')) {
      appendExternalLink(parent, match[1], token.slice(1, token.indexOf(']')));
    } else if (token.startsWith('http://') || token.startsWith('https://')) {
      // Keep common sentence punctuation outside the link. Parentheses that are
      // part of a URL remain supported unless they are the final character.
      const href = token.replace(/[.,;:!?\])}]+$/g, '');
      appendExternalLink(parent, href);
      parent.append(document.createTextNode(token.slice(href.length)));
    } else {
      const expression = token.startsWith('\\(') ? match[2] : match[3];
      appendMath(parent, expression, false, token);
    }
    offset = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function splitTableRow(line) {
  const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells = [];
  let cell = '';
  let escaped = false;
  let inCode = false;
  for (const character of trimmed) {
    if (escaped) {
      cell += character;
      escaped = false;
    } else if (character === '\\') {
      cell += character;
      escaped = true;
    } else if (character === '`') {
      cell += character;
      inCode = !inCode;
    } else if (character === '|' && !inCode) {
      cells.push(cell.trim());
      cell = '';
    } else {
      cell += character;
    }
  }
  cells.push(cell.trim());
  return cells;
}

function tableAlignments(line) {
  const cells = splitTableRow(line);
  if (!cells.length || cells.some((cell) => !/^:?-{3,}:?$/.test(cell))) return undefined;
  return cells.map((cell) => cell.startsWith(':') && cell.endsWith(':') ? 'center'
    : cell.endsWith(':') ? 'right' : cell.startsWith(':') ? 'left' : undefined);
}

function renderMarkdownTable(parent, headers, alignments, rows) {
  const wrapper = document.createElement('div');
  wrapper.className = 'markdown-table-wrap';
  const table = document.createElement('table');
  const head = document.createElement('thead');
  const headerRow = document.createElement('tr');
  headers.forEach((text, index) => {
    const cell = document.createElement('th');
    if (alignments[index]) cell.style.textAlign = alignments[index];
    appendInline(cell, text);
    headerRow.append(cell);
  });
  head.append(headerRow);
  table.append(head);
  const body = document.createElement('tbody');
  rows.forEach((values) => {
    const row = document.createElement('tr');
    headers.forEach((_, index) => {
      const cell = document.createElement('td');
      if (alignments[index]) cell.style.textAlign = alignments[index];
      appendInline(cell, values[index] || '');
      row.append(cell);
    });
    body.append(row);
  });
  table.append(body);
  wrapper.append(table);
  parent.append(wrapper);
}

function renderMarkdown(parent, markdown) {
  const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
  let code;
  let codeLanguage;
  let paragraph;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.startsWith('```')) {
      if (code) {
        if (codeLanguage === 'mermaid') {
          const source = code.textContent.replace(/\n$/, '');
          const diagram = document.createElement('div');
          diagram.className = 'mermaid-diagram';
          code.parentElement.replaceWith(diagram);
          void renderMermaid(diagram, source);
        }
        code = undefined;
        codeLanguage = undefined;
      } else {
        const pre = document.createElement('pre');
        code = document.createElement('code');
        codeLanguage = line.slice(3).trim().toLowerCase();
        pre.append(code);
        parent.append(pre);
      }
      paragraph = undefined;
      continue;
    }
    if (code) {
      code.append(document.createTextNode(`${line}\n`));
      continue;
    }

    const trimmed = line.trim();
    const displayDelimiter = trimmed.startsWith('$$') ? '$$'
      : trimmed.startsWith('\\[') ? '\\]' : undefined;
    if (displayDelimiter) {
      const opening = displayDelimiter === '$$' ? '$$' : '\\[';
      const closing = displayDelimiter;
      let expression = trimmed.slice(opening.length);
      const sourceLines = [line];
      if (expression.endsWith(closing) && expression.length >= closing.length) {
        expression = expression.slice(0, -closing.length);
      } else {
        const expressionLines = [expression];
        while (index + 1 < lines.length) {
          index += 1;
          const mathLine = lines[index];
          sourceLines.push(mathLine);
          if (mathLine.trimEnd().endsWith(closing)) {
            expressionLines.push(mathLine.trimEnd().slice(0, -closing.length));
            break;
          }
          expressionLines.push(mathLine);
        }
        expression = expressionLines.join('\n');
      }
      appendMath(parent, expression.trim(), true, sourceLines.join('\n'));
      paragraph = undefined;
      continue;
    }
    if (!trimmed) {
      paragraph = undefined;
      continue;
    }
    if (line.includes('|') && index + 1 < lines.length) {
      const alignments = tableAlignments(lines[index + 1]);
      const headers = splitTableRow(line);
      if (alignments && headers.length === alignments.length) {
        const rows = [];
        index += 1;
        while (index + 1 < lines.length && lines[index + 1].includes('|')) {
          const values = splitTableRow(lines[index + 1]);
          if (values.length !== headers.length) break;
          rows.push(values);
          index += 1;
        }
        renderMarkdownTable(parent, headers, alignments, rows);
        paragraph = undefined;
        continue;
      }
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length + 2}`);
      element.dataset.tocLevel = heading[1].length;
      element.dataset.tocText = heading[2].replace(/[`*_\[\]]/g, '').trim();
      element.id = `heading-${parent.closest('.message')?.dataset.messageId || 'message'}-${parent.querySelectorAll('[data-toc-level]').length + 1}`;
      appendInline(element, heading[2]);
      parent.append(element);
      paragraph = undefined;
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      let list = parent.lastElementChild;
      if (!list || list.tagName !== 'UL') {
        list = document.createElement('ul');
        parent.append(list);
      }
      const item = document.createElement('li');
      appendInline(item, bullet[1]);
      list.append(item);
      paragraph = undefined;
      continue;
    }
    if (!paragraph) {
      paragraph = document.createElement('p');
      parent.append(paragraph);
    } else {
      paragraph.append(document.createElement('br'));
    }
    appendInline(paragraph, line);
  }
}

function setMessageRaw(article, raw) {
  const id = article.dataset.messageId;
  const toggle = article.querySelector('.message-view-toggle');
  const rendered = article.querySelector('.message-content');
  const source = article.querySelector('.message-raw');
  article.dataset.raw = String(raw);
  toggle.textContent = raw ? 'Rendered' : 'Raw';
  toggle.title = raw ? 'Show rendered message' : 'Show raw message text';
  toggle.setAttribute('aria-pressed', String(raw));
  rendered.hidden = raw;
  source.hidden = !raw;
  if (id) {
    if (raw) rawMessageIds.add(id);
    else rawMessageIds.delete(id);
  }
}

function messageExcerpt(markdown) {
  const excerpt = markdown
    .replace(/```[^\n]*\n?/g, ' ')
    .replace(/[#>*_`\[\]()~-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return excerpt || '(No text content)';
}

function setMessageCollapsed(article, collapsed) {
  const id = article.dataset.messageId;
  article.dataset.collapsed = String(collapsed);
  const toggle = article.querySelector('.message-toggle');
  toggle.setAttribute('aria-expanded', String(!collapsed));
  toggle.title = collapsed ? 'Expand message' : 'Collapse message';
  toggle.textContent = collapsed ? '▸' : '▾';
  if (id) {
    if (collapsed) {
      collapsedMessageIds.add(id);
      expandedMessageIds.delete(id);
    } else {
      collapsedMessageIds.delete(id);
      expandedMessageIds.add(id);
    }
  }
}

function syncGlobalCollapseButton() {
  const articles = [...messages.querySelectorAll('.message')];
  allMessagesCollapsed = articles.length > 0 && articles.every(
    (article) => article.dataset.collapsed === 'true',
  );
  toggleAllMessages.textContent = allMessagesCollapsed ? 'Expand all' : 'Collapse all';
  toggleAllMessages.setAttribute('aria-pressed', String(allMessagesCollapsed));
}

function toggleAll() {
  const collapse = !allMessagesCollapsed;
  messages.querySelectorAll('.message').forEach(
    (article) => setMessageCollapsed(article, collapse),
  );
  syncGlobalCollapseButton();
  renderMessageToc(currentMessageArticle());
}

function closeToc() {
  messageToc.dataset.open = 'false';
  tocToggle.setAttribute('aria-expanded', 'false');
}

function scrollToHeading(heading) {
  heading.scrollIntoView({
    behavior: reducedMotion.matches ? 'auto' : 'smooth',
    block: 'start',
  });
  if (mobileToc.matches) closeToc();
}

function syncActiveTocHeading(article) {
  const headings = article ? [...article.querySelectorAll('[data-toc-level]')] : [];
  if (!headings.length) return;
  const headerOffset = document.body.querySelector(':scope > header')?.offsetHeight || 0;
  let active = headings[0];
  for (const heading of headings) {
    if (heading.getBoundingClientRect().top <= headerOffset + 12) active = heading;
    else break;
  }
  let activeButton;
  messageTocItems.querySelectorAll('button').forEach((button) => {
    const activeButtonMatch = button.dataset.headingId === active.id;
    button.setAttribute('aria-current', String(activeButtonMatch));
    if (activeButtonMatch) activeButton = button;
  });
  if (activeButton) {
    const itemTop = activeButton.offsetTop;
    const itemBottom = itemTop + activeButton.offsetHeight;
    const visibleTop = messageTocItems.scrollTop;
    const visibleBottom = visibleTop + messageTocItems.clientHeight;
    if (itemTop < visibleTop) {
      messageTocItems.scrollTop = Math.max(0, itemTop - 8);
    } else if (itemBottom > visibleBottom) {
      messageTocItems.scrollTop = itemBottom - messageTocItems.clientHeight + 8;
    }
  }
}

function renderMessageToc(article) {
  const available = article
    && article.dataset.collapsed !== 'true'
    && article.dataset.raw !== 'true';
  const headings = available ? [...article.querySelectorAll('[data-toc-level]')] : [];
  messageTocItems.replaceChildren();
  messageToc.hidden = headings.length === 0;
  tocToggle.hidden = headings.length === 0;
  if (!headings.length) {
    closeToc();
    return;
  }
  headings.forEach((heading) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.level = heading.dataset.tocLevel;
    button.dataset.headingId = heading.id;
    button.textContent = heading.dataset.tocText || heading.textContent;
    button.addEventListener('click', () => scrollToHeading(heading));
    messageTocItems.append(button);
  });
  syncActiveTocHeading(article);
}

function currentMessageArticle() {
  return [...messages.querySelectorAll('.message')][currentMessageIndex];
}

function syncMessageNavigation(index = currentMessageIndex) {
  const articles = [...messages.querySelectorAll('.message')];
  if (!articles.length) {
    messageNavigation.hidden = true;
    return;
  }
  currentMessageIndex = Math.max(0, Math.min(index, articles.length - 1));
  messageNavigation.hidden = false;
  previousMessage.disabled = currentMessageIndex === 0;
  nextMessage.disabled = currentMessageIndex === articles.length - 1;
  messagePosition.value = `${currentMessageIndex + 1} / ${articles.length}`;
  messagePosition.textContent = messagePosition.value;
  renderMessageToc(articles[currentMessageIndex]);
}

function navigateMessage(offset) {
  const articles = [...messages.querySelectorAll('.message')];
  if (!articles.length) return;
  const nextIndex = Math.max(0, Math.min(currentMessageIndex + offset, articles.length - 1));
  currentMessageIndex = nextIndex;
  articles[nextIndex].scrollIntoView({
    behavior: reducedMotion.matches ? 'auto' : 'smooth',
    block: 'start',
  });
  syncMessageNavigation();
}

function observeVisibleMessage() {
  const articles = [...messages.querySelectorAll('.message')];
  if (!articles.length) return;
  const headerOffset = document.body.querySelector(':scope > header')?.offsetHeight || 0;
  const readingLine = headerOffset + 12;
  // Keep a long message active for as long as it crosses the reading line.
  // Choosing whichever message top is closest switches prematurely to the next
  // card when the reader is deep inside a long response.
  let bestIndex = articles.findIndex((article) => {
    const rect = article.getBoundingClientRect();
    return rect.top <= readingLine && rect.bottom > readingLine;
  });
  if (bestIndex < 0) {
    bestIndex = articles.findIndex(
      (article) => article.getBoundingClientRect().bottom > readingLine,
    );
  }
  if (bestIndex < 0) bestIndex = articles.length - 1;
  if (bestIndex !== currentMessageIndex) syncMessageNavigation(bestIndex);
}

function captureScrollAnchor() {
  const distanceFromBottom = document.documentElement.scrollHeight
    - (window.scrollY + window.innerHeight);
  const followLatest = distanceFromBottom < 80;
  if (followLatest) return { followLatest: true };
  const headerOffset = document.body.querySelector(':scope > header')?.offsetHeight || 0;
  const article = [...messages.querySelectorAll('.message')].find(
    (candidate) => candidate.getBoundingClientRect().bottom > headerOffset,
  );
  return article ? {
    followLatest: false,
    messageId: article.dataset.messageId,
    top: article.getBoundingClientRect().top,
  } : { followLatest: false, scrollY: window.scrollY };
}

function restoreScrollAnchor(anchor) {
  if (anchor.followLatest) {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'instant' });
    return;
  }
  const article = anchor.messageId
    ? [...messages.querySelectorAll('.message')].find(
      (candidate) => candidate.dataset.messageId === anchor.messageId,
    ) : undefined;
  if (article) {
    window.scrollBy({ top: article.getBoundingClientRect().top - anchor.top, behavior: 'instant' });
  } else if (Number.isFinite(anchor.scrollY)) {
    window.scrollTo({ top: anchor.scrollY, behavior: 'instant' });
  }
}

function render(documentData) {
  const scrollAnchor = captureScrollAnchor();
  const previousStreaming = messages.querySelector('.message[data-streaming="true"]');
  const streamingWasExpanded = previousStreaming?.dataset.collapsed === 'false';
  const streamingMarkdown = previousStreaming?.querySelector('.message-raw code')?.textContent || '';
  pinPiSession(documentData.session_id);
  if (documentData.pane_id) resolvedPane = documentData.pane_id;
  const nextFingerprint = JSON.stringify(documentData);
  if (nextFingerprint === fingerprint) return;
  fingerprint = nextFingerprint;
  detail.textContent = documentData.document_name
    ? `${documentData.document_name} · ${documentData.cwd || 'Project root'}`
    : documentData.cwd || documentData.session_id || '';
  if (documentData.document_mode) {
    document.title = `${documentData.document_name} · Markdown preview`;
    scratchpadOpen.hidden = true;
    filesOpen.hidden = false;
    toggleAllMessages.hidden = true;
    liveState.textContent = 'Document';
  }
  sessionSize.textContent = formatBytes(documentData.session_bytes);
  sessionSize.title = `Raw pi session JSONL: ${Number.isFinite(documentData.session_bytes) ? documentData.session_bytes.toLocaleString() : 'unknown'} bytes`;
  liveState.dataset.live = String(documentData.live);
  liveState.textContent = documentData.document_mode
    ? 'Document' : documentData.live ? 'Live' : 'Persisted';
  if (streamingWasExpanded && !documentData.messages.some((message) => message.streaming)) {
    const finalized = [...documentData.messages].reverse().find((message) => (
      message.role === 'assistant'
      && (message.markdown.startsWith(streamingMarkdown) || streamingMarkdown.startsWith(message.markdown))
    ));
    if (finalized) expandedMessageIds.add(finalized.id);
  }
  const nodes = documentData.messages.map((message) => {
    const node = template.content.cloneNode(true);
    const article = node.querySelector('article');
    article.dataset.role = message.role;
    article.dataset.streaming = String(message.streaming);
    article.dataset.messageId = message.id;
    const collapsed = documentData.document_mode ? false : !expandedMessageIds.has(message.id);
    setMessageCollapsed(article, collapsed);
    setMessageRaw(article, rawMessageIds.has(message.id));
    article.querySelector('.message-toggle').addEventListener('click', () => {
      setMessageCollapsed(article, article.dataset.collapsed !== 'true');
      syncGlobalCollapseButton();
      renderMessageToc(article);
    });
    article.querySelector('.message-view-toggle').addEventListener('click', () => {
      setMessageRaw(article, article.dataset.raw !== 'true');
      renderMessageToc(article);
    });
    node.querySelector('strong').textContent = message.role === 'user'
      ? 'You' : message.role === 'document' ? documentData.document_name : 'Pi';
    node.querySelector('time').textContent = formatTime(message.timestamp);
    node.querySelector('.message-peek').textContent = messageExcerpt(message.markdown);
    node.querySelector('.message-raw code').textContent = message.markdown;
    renderMarkdown(node.querySelector('.message-content'), message.markdown);
    return node;
  });
  messages.replaceChildren(...nodes);
  messages.setAttribute('aria-busy', 'false');
  const articles = [...messages.querySelectorAll('.message')];
  const anchoredIndex = scrollAnchor.messageId
    ? articles.findIndex((article) => article.dataset.messageId === scrollAnchor.messageId)
    : -1;
  currentMessageIndex = scrollAnchor.followLatest
    ? Math.max(0, documentData.messages.length - 1)
    : anchoredIndex >= 0
      ? anchoredIndex
      : Math.max(0, Math.min(currentMessageIndex, documentData.messages.length - 1));
  syncMessageNavigation();
  syncGlobalCollapseButton();
  restoreScrollAnchor(scrollAnchor);
}

async function refresh() {
  if (!backend && !piSessionId) {
    messages.textContent = 'No pi session was selected.';
    return;
  }
  try {
    const response = await fetch(apiUrl(), { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
    render(data);
    refreshDelay = data.live ? 1_000 : 3_000;
  } catch (error) {
    refreshDelay = 3_000;
    if (!fingerprint) messages.textContent = error.message;
    liveState.dataset.live = 'false';
    liveState.textContent = 'Disconnected';
  }
}

async function poll() {
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  if (document.hidden) return;
  await refresh();
  if (!document.hidden) refreshTimer = setTimeout(poll, refreshDelay);
}

function resumePolling() {
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  if (!document.hidden) void poll();
}

tocToggle.addEventListener('click', () => {
  const open = messageToc.dataset.open !== 'true';
  messageToc.dataset.open = String(open);
  tocToggle.setAttribute('aria-expanded', String(open));
});
tocClose.addEventListener('click', closeToc);
filesOpen.addEventListener('click', () => void openFiles());
filesClose.addEventListener('click', closeFiles);
filesDialog.addEventListener('click', (event) => {
  if (event.target === filesDialog) closeFiles();
});
filesSearch.addEventListener('input', renderMarkdownFiles);
scratchpadOpen.addEventListener('click', () => void openScratchpad());
scratchpadClose.addEventListener('click', closeScratchpad);
scratchpadDialog.addEventListener('click', (event) => {
  if (event.target === scratchpadDialog) closeScratchpad();
});
scratchpadEditor.addEventListener('input', () => {
  renderScratchpad();
  queueScratchpadSave();
});
scratchpadToolbar.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (button) applyScratchpadFormatting(button);
});
themeToggle.addEventListener('click', () => {
  const next = document.documentElement.dataset.previewTheme === 'light' ? 'dark' : 'light';
  applyTheme(next, true);
});
systemLightTheme.addEventListener('change', () => {
  if (!localStorage.getItem(THEME_KEY)) applyTheme(preferredTheme());
});
toggleAllMessages.addEventListener('click', toggleAll);
previousMessage.addEventListener('click', () => navigateMessage(-1));
nextMessage.addEventListener('click', () => navigateMessage(1));
let scrollFrame;
window.addEventListener('scroll', () => {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = undefined;
    observeVisibleMessage();
    syncActiveTocHeading(currentMessageArticle());
  });
}, { passive: true });
document.addEventListener('keydown', (event) => {
  if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    event.preventDefault();
    navigateMessage(event.key === 'ArrowUp' ? -1 : 1);
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(refreshTimer);
    refreshTimer = undefined;
  } else {
    resumePolling();
  }
});

await poll();
