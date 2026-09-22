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
const keydownHandler = app.slice(
  app.indexOf('  function handleMobileTerminalKeyDown(event)'),
  app.indexOf('  function handleMobileTerminalBeforeInput(event)'),
);

function harness(text = 'abcdef', cursor = text.length, staticPrefix = '', options = {}) {
  const sent = [];
  const reports = [];
  const modifierInputs = [];
  const modifierConsumed = [];
  const timers = [];
  let value = staticPrefix + text;
  const helper = {
    get value() { return value; },
    set value(next) {
      value = next;
      this.selectionStart = this.selectionEnd = next.length;
      this.selectionDirection = 'none';
    },
    selectionStart: staticPrefix.length + cursor,
    selectionEnd: staticPrefix.length + cursor,
    selectionDirection: 'none',
    setSelectionRange(start, end, direction = 'none') {
      this.selectionStart = start;
      this.selectionEnd = end;
      this.selectionDirection = direction;
    },
    setAttribute() {},
    removeAttribute() {},
  };
  let rendered = staticPrefix + text;
  const buffer = {
    baseY: 0, cursorY: 0, cursorX: staticPrefix.length + cursor,
    getLine(row) {
      return row === 0 ? {
        translateToString: (_trim, start = 0, end = rendered.length) => rendered.slice(start, end),
      } : undefined;
    },
  };
  const pane = {
    mode: 'control', closed: false,
    terminal: { textarea: helper, modes: {}, buffer: { active: buffer }, clearSelection() {} },
    mobilePredictionPrefix: staticPrefix,
    mobilePredictionText: text,
    mobilePredictionCursor: cursor,
    mobilePredictionConfirmed: true,
    mobilePredictionPending: false,
  };
  const context = vm.createContext({
    ...prediction, pane, helper,
    document: { activeElement: helper },
    iosKeyboard: true,
    nativeKeyboardInput: true,
    mobileQuery: { matches: true },
    mobileKeyboardLocked: false,
    mobileModifierState: { control: false, alt: false, shift: false },
    mobileModifierMode: { control: 'off', alt: 'off', shift: 'off' },
    consumeMobileModifiers: () => { modifierConsumed.push(true); },
    performance: { now: () => 1000 },
    MOBILE_BACKSPACE_SENTINEL: ' ',
    MOBILE_BACKSPACE_BEFORE_INPUT_SUPPRESSION_MS: 200,
    paneForKeyboardTarget: (target) => target === helper ? pane : undefined,
    paneAcceptsInput: (candidate) => candidate.mode === 'control' && !candidate.closed,
    setActivePane: () => true,
    sendInput: (data) => sent.push(data),
    sendMobilePaneKeyboardData: (candidate, data) => {
      if (candidate.mode !== 'control' || candidate.closed) return false;
      sent.push(data);
      return true;
    },
    applyMobileModifiers: (data) => {
      modifierInputs.push(data);
      return options.modifierConversion ? options.modifierConversion(data) : data;
    },
    showBrowserToast() {},
    reportClientIssue: (kind, detail) => { reports.push({ kind, detail }); },
    clearTimeout() {},
    setTimeout: (callback) => timers.push(callback),
    ...options,
  });
  vm.runInContext(handlers, context);
  vm.runInContext(keydownHandler, context);
  const event = (fields = {}) => ({
    target: helper, stopImmediatePropagation() {}, ...fields,
  });
  return {
    pane, helper, context, sent, reports, modifierInputs, modifierConsumed,
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

test('a caret swipe follows the native caret while an echo is still pending', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.pane.mobilePredictionConfirmed = false;
  h.pane.mobilePredictionPending = true;
  h.selection('prompt> '.length + 2);
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(3)]);
  assert.equal(h.pane.mobilePredictionCursor, 2);
});

test('a blocked caret swipe reports the gate that refuses it', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.pane.mode = 'observe';
  h.selection('prompt> '.length + 2);
  assert.deepEqual(h.sent, []);
  const reports = h.reports.filter((entry) => entry.kind === 'caret-blocked');
  assert.equal(reports.length, 1);
  assert.match(reports[0].detail, /read-only/);
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

test('a native helper edit sends only its changed terminal range', () => {
  const staticPrefix = 'prompt> ';
  const h = harness('hello', 5, staticPrefix);
  h.selection(staticPrefix.length + 2);
  h.input(`${staticPrefix}heXllo`, staticPrefix.length + 3);
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(3), 'X']);
  assert.equal(h.pane.mobilePredictionText, 'heXllo');
  assert.equal(h.pane.mobilePredictionCursor, 3);

  h.sent.length = 0;
  h.input('changed> heXllo', 9);
  assert.deepEqual(h.sent, []);
  assert.equal(h.helper.value, `${staticPrefix}heXllo`);
  assert.equal(h.helper.selectionStart, staticPrefix.length + 3);
});

test('terminal output refreshes context and preserves the native selection', () => {
  const h = harness('hello', 5, 'old> ');
  h.helper.setSelectionRange(7, 7);
  h.render('new prompt> hello', 17);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.pane.mobilePredictionPrefix, 'new prompt> ');
  assert.equal(h.helper.value, 'new prompt> hello');
  assert.equal(h.helper.selectionStart, 14);
  assert.deepEqual(h.sent, []);
});

test('an empty shell buffer still gets terminal context for the first swipe', () => {
  const h = harness('', 0);
  h.render('prompt> ', 8);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.pane.mobilePredictionConfirmed, true);
  assert.equal(h.pane.mobilePredictionPrefix, '');
  assert.equal(h.pane.mobilePredictionText, 'prompt> ');
  assert.equal(h.helper.value, 'prompt> ');
  assert.equal(h.helper.selectionStart, 8);
  h.input('prompt>  hello', 14, { data: 'hello' });
  assert.deepEqual(h.sent, ['hello']);
  assert.equal(h.helper.value, 'prompt> hello');
  assert.equal(h.pane.mobilePredictionText, 'prompt> hello');
});

test('typing after middle Backspace preserves context and forward caret movement', () => {
  const staticPrefix = 'prompt> ';
  const h = harness("one I’d something", 7, staticPrefix);
  h.sent.length = 0;
  h.input(`${staticPrefix}one I’ something`, staticPrefix.length + 6, {
    inputType: 'deleteContentBackward',
  });
  h.input(`${staticPrefix}one I something`, staticPrefix.length + 5, {
    inputType: 'deleteContentBackward',
  });
  h.input(`${staticPrefix}one  something`, staticPrefix.length + 4, {
    inputType: 'deleteContentBackward',
  });
  assert.equal(h.pane.mobilePredictionText, 'one  something');
  assert.equal(h.pane.mobilePredictionCursor, 4);
  assert.equal(h.helper.value, `${staticPrefix}one  something`);
  assert.equal(h.helper.selectionStart, staticPrefix.length + 4);

  h.input(`${staticPrefix}one is something`, staticPrefix.length + 6, { data: 'is' });
  assert.deepEqual(h.sent, ['\x7f', '\x7f', '\x7f', 'is']);
  assert.equal(h.helper.value, `${staticPrefix}one is something`);
  assert.equal(h.pane.mobilePredictionText, 'one is something');
  assert.equal(h.pane.mobilePredictionCursor, 6);

  h.render(`${staticPrefix}one is something`, staticPrefix.length + 6);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  h.selection(staticPrefix.length + 'one is something'.length);
  assert.deepEqual(h.sent, ['\x7f', '\x7f', '\x7f', 'is', '\x1b[C'.repeat(10)]);
});

test('continuous deletion keeps known text across an unmarked physical wrap', () => {
  const text = '0123456789abcdefghij';
  const h = harness(text);
  h.input(text.slice(0, -1), text.length - 1, { inputType: 'deleteContentBackward' });
  h.render('abcdefghi', 9);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.helper.value, '0123456789abcdefghi');
  assert.equal(h.pane.mobilePredictionPending, true);
  for (let length = text.length - 2; length >= 7; length -= 1) {
    h.input(text.slice(0, length), length, { inputType: 'deleteContentBackward' });
  }
  assert.equal(h.helper.value, '0123456');
  assert.equal(h.pane.mobilePredictionText, '0123456');
  assert.deepEqual(h.sent, Array(13).fill('\x7f'));
});

test('Backspace passes through when a pending known shadow reaches its start', () => {
  const h = harness('', 0);
  h.pane.mobilePredictionConfirmed = false;
  h.pane.mobilePredictionPending = true;
  let prevented = false;
  h.context.handleMobileTerminalKeyDown(h.event({
    key: 'Backspace', isComposing: false,
    preventDefault() { prevented = true; },
  }));
  assert.equal(prevented, true);
  assert.deepEqual(h.sent, ['\x7f']);
});

test('an empty shadow keeps the helper empty until typing', () => {
  const h = harness('', 0);
  h.render('', 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  assert.equal(h.helper.value, '');
  h.input('hello', 5, { data: 'hello' });
  assert.deepEqual(h.sent, ['hello']);
  assert.equal(h.helper.value, 'hello');
});

test('an Android profile keeps the empty helper free of a marker', () => {
  const h = harness('', 0, '', { iosKeyboard: false });
  h.render('', 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  assert.equal(h.helper.value, '');
  assert.ok(!h.pane.mobileBackspaceSentinel);
});

test('an IME key event is stopped only while the shadow owns composition', () => {
  const h = harness();
  const event = (stopped) => ({
    target: h.helper, key: 'Unidentified', keyCode: 229, isComposing: true,
    stopImmediatePropagation() { stopped.value = true; },
  });
  const stopped = { value: false };
  h.pane.mobilePredictionComposition = {};
  h.context.handleMobileTerminalKeyDown(event(stopped));
  assert.equal(stopped.value, true);
  stopped.value = false;
  h.pane.mobilePredictionComposition = undefined;
  h.context.handleMobileTerminalKeyDown(event(stopped));
  assert.equal(stopped.value, false);
});

test('returned terminal cells replace a rejected native proposal', () => {
  const h = harness();
  h.render('new prompt> ');
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.helper.value, 'new prompt> ');
  assert.equal(h.pane.mobilePredictionPrefix, '');
  assert.equal(h.pane.mobilePredictionText, 'new prompt> ');
  assert.equal(h.pane.mobilePredictionConfirmed, true);
  h.input('new prompt> Q', 13);
  h.input('new prompt> Q', 13, { inputType: undefined });
  assert.deepEqual(h.sent, ['Q']);
});

test('partial shell echoes do not swallow fast native input', () => {
  const h = harness();
  h.input('abcdefghi', 9);
  h.render('abcdefg', 7);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.helper.value, 'abcdefghi');
  assert.equal(h.pane.mobilePredictionText, 'abcdefghi');
  assert.equal(h.pane.mobilePredictionConfirmed, false);
  h.render('abcdefghi', 9);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.helper.value, 'abcdefghi');
  assert.equal(h.pane.mobilePredictionConfirmed, true);
  assert.deepEqual(h.sent, ['ghi']);
});

test('delayed caret echoes keep the complete owned text confirmed', () => {
  const h = harness();
  h.selection(1);
  h.context.syncMobilePredictionFromTerminal(h.pane);
  assert.equal(h.pane.mobilePredictionConfirmed, true);
  h.selection(3);
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(5), '\x1b[C'.repeat(2)]);
});

test('composition uses terminal context but sends only changed text', () => {
  const h = harness('', 0, 'prompt> ');
  h.context.handleMobilePredictionCompositionStart(h.event());
  h.input('prompt> 日本', 10, { inputType: 'insertCompositionText', isComposing: true });
  h.context.handleMobilePredictionCompositionEnd(h.event({ data: '日本' }));
  h.flush();
  assert.deepEqual(h.sent, ['日本']);
  assert.equal(h.helper.value, 'prompt> 日本');
  assert.equal(h.pane.mobilePredictionText, '日本');
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

test('a composition start never rewrites the helper', () => {
  const h = harness('', 0, '', { iosKeyboard: true });
  h.render('', 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  assert.equal(h.helper.value, '');
  // The IME applies the first character, then reports the composition.
  h.helper.value = ' h';
  h.helper.setSelectionRange(2, 2);
  h.context.handleMobilePredictionCompositionStart(h.event());
  assert.equal(h.helper.value, ' h');
  assert.equal(h.pane.mobilePredictionComposition, true);
  assert.deepEqual(h.sent, []);
});

test('a composition commits the first letter once', () => {
  const h = harness('', 0, '', { iosKeyboard: true });
  h.render('', 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  h.context.handleMobilePredictionCompositionStart(h.event());
  assert.ok(h.pane.mobilePredictionComposition);
  h.input('h', 1, { inputType: 'insertCompositionText', isComposing: true });
  h.context.handleMobilePredictionCompositionEnd(h.event({ data: 'h' }));
  h.flush();
  assert.deepEqual(h.sent, ['h']);
  assert.equal(h.helper.value, 'h');
});

test('an IME caret before the typed character does not pull the cursor back', () => {
  const h = harness('', 0, '', { iosKeyboard: true });
  h.render('', 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  assert.equal(h.helper.value, '');
  // The IME writes the letter and reports the caret before it.
  h.input('y', 0);
  assert.deepEqual(h.sent, ['y']);
  assert.equal(h.pane.mobilePredictionCursor, 1);
});

test('a modifier converts one inserted character after a caret anomaly', () => {
  const h = harness('', 0, '', {
    iosKeyboard: true,
    mobileModifierState: { control: true, alt: false, shift: false },
    modifierConversion: (data) => (data === 'y' ? '\x19' : data),
  });
  h.render('', 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  h.input('y', 0);
  assert.deepEqual(h.sent, ['\x19']);
  assert.equal(h.modifierConsumed.length, 1);
});

test('a modifier drops the separator space an IME appends', () => {
  const h = harness('', 0, '', {
    iosKeyboard: true,
    mobileModifierState: { control: true, alt: false, shift: false },
    modifierConversion: (data) => (data === 'y' ? '\x19' : data),
  });
  h.render('', 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  h.input('y ', 2);
  assert.deepEqual(h.sent, ['\x19']);
  assert.equal(h.modifierConsumed.length, 1);
});

test('composition keeps the known input-session shadow across concurrent output', () => {
  const h = harness();
  h.helper.setSelectionRange(1, 3);
  h.context.handleMobilePredictionCompositionStart(h.event());
  h.render('new prompt> ');
  h.input('a日def', 2, { inputType: 'insertCompositionText', isComposing: true });
  h.context.handleMobilePredictionCompositionEnd(h.event({ data: '日' }));
  h.flush();
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(3) + '\x7f\x7f日']);
  assert.equal(h.helper.value, 'a日def');
  assert.equal(h.pane.mobilePredictionPending, true);
});
