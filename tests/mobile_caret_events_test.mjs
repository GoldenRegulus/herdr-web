import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as prediction from '../herdr_web/static/mobile-prediction.js';

// Exercise the application's event handlers without adding production test hooks.
// Browser replay separately checks xterm and native DOM event delivery.
const app = readFileSync(new URL('../herdr_web/static/app.js', import.meta.url), 'utf8');
const handlers = app.slice(
  app.indexOf('  function paneKeyboardHelper(pane)'),
  app.indexOf('  function resetPaneKeyboardHelper(pane)'),
);

function harness(text = 'abcdef', cursor = text.length) {
  const sent = [];
  const timers = [];
  let value = text;
  const helper = {
    get value() { return value; },
    set value(next) {
      value = next;
      this.selectionStart = this.selectionEnd = next.length;
      this.selectionDirection = 'none';
    },
    selectionStart: cursor,
    selectionEnd: cursor,
    selectionDirection: 'none',
    setSelectionRange(start, end, direction = 'none') {
      this.selectionStart = start;
      this.selectionEnd = end;
      this.selectionDirection = direction;
    },
    setAttribute() {},
    removeAttribute() {},
  };
  let rendered = text;
  const buffer = {
    baseY: 0, cursorY: 0, cursorX: cursor,
    getLine(row) {
      return row === 0 ? {
        translateToString: (_trim, start = 0, end = rendered.length) => rendered.slice(start, end),
      } : undefined;
    },
  };
  const pane = {
    mode: 'control', closed: false,
    terminal: { textarea: helper, modes: {}, buffer: { active: buffer }, clearSelection() {} },
    mobilePredictionText: text,
    mobilePredictionCursor: cursor,
    mobilePredictionConfirmed: true,
    mobilePredictionInvalidated: false,
  };
  const context = vm.createContext({
    ...prediction, pane, helper,
    document: { activeElement: helper },
    iosKeyboard: true,
    mobileQuery: { matches: true },
    mobileKeyboardLocked: false,
    MOBILE_BACKSPACE_SENTINEL: 'x',
    paneForKeyboardTarget: (target) => target === helper ? pane : undefined,
    paneAcceptsInput: (candidate) => candidate.mode === 'control' && !candidate.closed,
    setActivePane: () => true,
    sendInput: (data) => sent.push(data),
    sendMobilePaneKeyboardData: (candidate, data) => {
      if (candidate.mode !== 'control' || candidate.closed) return false;
      sent.push(data);
      return true;
    },
    applyMobileModifiers: (data) => data,
    showBrowserToast() {},
    setTimeout: (callback) => timers.push(callback),
  });
  vm.runInContext(handlers, context);
  const event = (fields = {}) => ({
    target: helper, stopImmediatePropagation() {}, ...fields,
  });
  return {
    pane, helper, context, sent,
    render(next, at = next.length) { rendered = next; buffer.cursorX = at; },
    selection(at) {
      helper.setSelectionRange(at, at);
      context.handleMobileCaretSelection();
    },
    input(next, at = next.length, fields = {}) {
      helper.value = next;
      helper.setSelectionRange(at, at);
      context.handleMobileTextInput(pane, event({ inputType: 'insertText', ...fields }));
    },
    event,
    flush() { while (timers.length) timers.shift()(); },
  };
}

test('selection events move once and text input uses the already-sent caret', () => {
  const h = harness();
  h.selection(2);
  h.context.handleMobileCaretSelection();
  h.selection(3);
  h.input('abcXdef', 4);
  h.context.handleMobileCaretSelection();
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(4), '\x1b[C', 'X']);
  assert.equal(h.pane.mobilePredictionCursor, 4);
});

test('input before selectionchange still moves to the native insertion position', () => {
  const h = harness();
  h.input('abXcdef', 3);
  h.context.handleMobileCaretSelection();
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(4) + 'X']);
});

test('barriers and non-collapsed selections do not move the terminal', () => {
  for (const change of [
    (h) => { h.pane.mode = 'observe'; },
    (h) => { h.pane.closed = true; },
    (h) => { h.pane.snapshot = {}; },
    (h) => { h.pane.mobileBackspaceSentinel = true; },
    (h) => { h.pane.mobileBackspacePreservedHelper = true; },
    (h) => { h.pane.mobilePredictionComposition = {}; },
    (h) => { h.context.mobileKeyboardLocked = true; },
    (h) => { h.context.iosKeyboard = false; },
    (h) => { h.helper.value = 'different'; },
    (h) => { h.context.document.activeElement = null; },
  ]) {
    const h = harness();
    change(h);
    h.selection(1);
    assert.deepEqual(h.sent, []);
  }
  const h = harness();
  h.helper.setSelectionRange(1, 3);
  h.context.handleMobileCaretSelection();
  assert.deepEqual(h.sent, []);
});

test('blur sends a final queued caret change and focus restores that position', () => {
  const h = harness();
  h.helper.setSelectionRange(2, 2);
  h.context.document.activeElement = null;
  h.context.preserveMobilePredictionBeforeBlur(h.pane);
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(4)]);
  h.render('abcdef', 2);
  h.helper.value = '';
  h.context.document.activeElement = h.helper;
  h.context.prepareMobilePredictionFocus(h.pane);
  assert.equal(h.helper.value, 'abcdef');
  assert.equal(h.helper.selectionStart, 2);
  h.context.handleMobileCaretSelection();
  assert.equal(h.sent.length, 1);
});

test('unrelated output disables stale movement and deletion without repeating helper text', () => {
  const h = harness();
  h.render('new prompt> ');
  h.context.syncMobilePredictionFromTerminal(h.pane);
  h.selection(2);
  h.input('abQef', 3);
  h.input('abQef', 3, { inputType: undefined });
  assert.deepEqual(h.sent, ['Q']);
  assert.equal(h.pane.mobilePredictionInvalidated, true);
  assert.equal(h.pane.mobilePredictionConfirmed, false);
});

test('delayed caret echoes keep the complete owned text confirmed', () => {
  const h = harness();
  h.selection(1);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.pane.mobilePredictionConfirmed, true);
  h.selection(3);
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(5), '\x1b[C'.repeat(2)]);
});

test('owned composition commits once at the moved caret', () => {
  const h = harness();
  h.selection(2);
  h.context.handleMobilePredictionCompositionStart(h.event());
  h.input('ab日本cdef', 4, { inputType: 'insertCompositionText', isComposing: true });
  h.context.handleMobilePredictionCompositionEnd(h.event({ data: '日本' }));
  h.input('ab日本cdef', 4);
  h.flush();
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(4), '日本']);
  assert.equal(h.pane.mobilePredictionCursor, 4);
});

test('output during composition cannot authorize stale deletion or movement', () => {
  const h = harness();
  h.helper.setSelectionRange(1, 3);
  h.context.handleMobilePredictionCompositionStart(h.event());
  h.render('new prompt> ');
  h.input('a日def', 2, { inputType: 'insertCompositionText', isComposing: true });
  h.context.handleMobilePredictionCompositionEnd(h.event({ data: '日' }));
  h.flush();
  assert.deepEqual(h.sent, ['日']);
  assert.equal(h.pane.mobilePredictionInvalidated, true);
});
