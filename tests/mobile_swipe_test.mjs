import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as prediction from '../herdr_web/static/mobile-prediction.js';
import {
  terminalDataForModifiedEnter,
  terminalDataForNavigationKey,
} from '../herdr_web/static/input-buffer.js';

const app = readFileSync(new URL('../herdr_web/static/app.js', import.meta.url), 'utf8');
const slice = (start, end) => {
  const from = app.indexOf(start);
  const to = app.indexOf(end);
  assert.ok(from !== -1 && to !== -1 && to > from, `cannot slice ${start}`);
  return app.slice(from, to);
};
const modifierFunctions = slice(
  '  function renderMobileModifierState()',
  '  function selectedPaneTerminal()',
);
const helperFunctions = slice(
  '  function paneKeyboardHelper(pane)',
  '  function handleMobilePredictionCompositionStart(event)',
);
const keyHandlers = slice(
  '  function sendMobileReturn(pane)',
  '  function terminalMouseButtonCode(button, motion = false)',
);

function harness(text = '', cursor = text.length, staticPrefix = '') {
  const sent = [];
  const report = [];
  let value = staticPrefix + text;
  const helper = {
    get value() { return value; },
    set value(next) {
      value = next;
      this.selectionStart = this.selectionEnd = next.length;
    },
    selectionStart: staticPrefix.length + cursor,
    selectionEnd: staticPrefix.length + cursor,
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
    },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    blur() {},
  };
  let rendered = staticPrefix + text;
  const buffer = {
    baseY: 0,
    cursorY: 0,
    cursorX: staticPrefix.length + cursor,
    getLine(row) {
      return row === 0
        ? {
          translateToString: (_trim, start = 0, end = rendered.length) => rendered.slice(start, end),
        }
        : undefined;
    },
  };
  const pane = {
    mode: 'control',
    closed: false,
    streamId: 'w1:p1',
    terminal: {
      textarea: helper,
      modes: {},
      buffer: { active: buffer },
      clearSelection() {},
    },
    mobilePredictionPrefix: staticPrefix,
    mobilePredictionText: text,
    mobilePredictionCursor: cursor,
    mobilePredictionConfirmed: true,
    mobilePredictionPending: false,
  };
  const context = vm.createContext({
    ...prediction,
    terminalDataForModifiedEnter,
    terminalDataForNavigationKey,
    pane,
    helper,
    document: {
      activeElement: helper,
      getSelection: () => '',
      documentElement: { dataset: { mobileKeyboard: 'open' } },
    },
    mobileModifiers: { querySelectorAll: () => [] },
    mobileNavigationModeButton: { setAttribute() {}, addEventListener() {} },
    mobileDefaultRow: { hidden: false },
    mobileNavigationRow: { hidden: true },
    mobileModifierState: { control: false, alt: false, shift: false },
    mobileModifierMode: { control: 'off', alt: 'off', shift: 'off' },
    mobileQuery: { matches: true },
    iosKeyboard: true,
    androidKeyboard: false,
    nativeKeyboardInput: true,
    mobileKeyboardLocked: false,
    mobileNavigationMode: false,
    terminal: { focus() {} },
    selectedPaneTerminal: () => pane,
    focusPaneKeyboard() {},
    stopMobileKeyRepeat() {},
    performance: { now: () => 1000 },
    MOBILE_BACKSPACE_BEFORE_INPUT_SUPPRESSION_MS: 200,
    MOBILE_COMPOSITION_STALL_MS: 750,
    MOBILE_RETURN_BEFORE_INPUT_SUPPRESSION_MS: 500,
    paneForKeyboardTarget: (target) => (target === helper ? pane : undefined),
    paneAcceptsInput: (candidate) => candidate.mode === 'control' && !candidate.closed,
    setActivePane: () => true,
    sendInput: (data) => { sent.push(data); },
    sendMobilePaneKeyboardData: (candidate, data) => {
      if (candidate.mode !== 'control' || candidate.closed) return false;
      sent.push(data);
      return true;
    },
    noteMobilePredictionTerminalData() {},
    reportClientIssue: (kind, detail) => { report.push({ kind, detail }); },
    restoreMobilePredictionHelper(candidate) {
      const prefixLength = (candidate.mobilePredictionPrefix || '').length;
      const at = prefixLength + candidate.mobilePredictionCursor;
      helper.value = (candidate.mobilePredictionPrefix || '') + candidate.mobilePredictionText;
      helper.setSelectionRange(at, at);
    },
    showBrowserToast() {},
    clearTimeout,
    setTimeout,
    requestAnimationFrame: (callback) => callback(),
  });
  vm.runInContext(modifierFunctions, context);
  vm.runInContext(helperFunctions, context);
  vm.runInContext(keyHandlers, context);
  const event = (fields = {}) => ({
    target: helper,
    isComposing: false,
    cancelable: true,
    preventDefault() {},
    stopImmediatePropagation() {},
    ...fields,
  });
  return {
    pane,
    helper,
    context,
    sent,
    report,
    render(next, at = next.length) { rendered = next; buffer.cursorX = at; },
    sync() { context.syncMobilePredictionFromTerminal(pane); },
    selection(at) {
      helper.setSelectionRange(at, at);
      context.handleMobileCaretSelection();
    },
    swipe(word, { type = 'insertText', composing = false } = {}) {
      const next = helper.value + word;
      const at = next.length;
      helper.value = next;
      helper.setSelectionRange(at, at);
      context.handleMobileTextInput(
        pane, event({ inputType: type, data: word, isComposing: composing }),
      );
    },
    input(next, at = next.length, fields = {}) {
      helper.value = next;
      helper.setSelectionRange(at, at);
      context.handleMobileTextInput(pane, event({ inputType: 'insertText', ...fields }));
    },
    text() { return helper.value; },
    typed() { return sent.join(''); },
    deleted() { return sent.some((entry) => entry.includes('\x7f')); },
  };
}

test('two swipes keep the first word when the terminal echo is immediate', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.swipe(' world');
  assert.equal(h.deleted(), false, 'a swipe must never emit a deletion');
  assert.equal(h.typed(), ' world');
  h.render('prompt> hello world', 19);
  h.sync();
  assert.equal(h.text(), 'prompt> hello world');
});

test('two swipes keep the first word while the echo is late', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.swipe(' world');
  h.render('prompt> h', 9);
  h.sync();
  assert.equal(h.text(), 'prompt> hello world', 'a partial echo must not drop native text');
  h.render('prompt> hello world', 19);
  h.sync();
  assert.equal(h.text(), 'prompt> hello world');
  assert.equal(h.deleted(), false);
});

test('two swipes keep the first word when the echo still shows the old line', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.swipe(' world');
  h.render('prompt> ', 8);
  h.sync();
  assert.equal(h.text(), 'prompt> hello world', 'a stale echo must not drop native text');
  assert.equal(h.deleted(), false);
});

test('a duplicated swipe event does not resend or delete the word', () => {
  const h = harness('', 0, 'prompt> ');
  h.swipe('hello');
  const afterFirst = h.sent.slice();
  h.input('prompt> hello', 13, { inputType: 'insertText', data: 'hello' });
  assert.deepEqual(h.sent, afterFirst, 'the duplicate must send nothing');
  h.swipe(' world');
  assert.equal(h.deleted(), false);
  assert.equal(h.text(), 'prompt> hello world');
});

test('a swipe that commits the word with its separator keeps both words', () => {
  const h = harness('', 0, 'prompt> ');
  h.swipe('hello ');
  h.swipe('world');
  assert.equal(h.text(), 'prompt> hello world');
  assert.equal(h.typed(), 'hello world');
  assert.equal(h.deleted(), false);
});

test('a swipe keeps the first word when the keyboard reports the caret at the word start', () => {
  const h = harness('hello', 5, 'prompt> ');
  const next = h.text() + ' world';
  h.helper.value = next;
  h.helper.setSelectionRange(14, 14);
  h.context.handleMobileTextInput(
    h.pane,
    { target: h.helper, inputType: 'insertText', data: ' world', isComposing: false, cancelable: true, preventDefault() {}, stopImmediatePropagation() {} },
  );
  h.context.handleMobileCaretSelection();
  assert.equal(h.deleted(), false, 'the caret anomaly must not rewrite the first word');
  assert.equal(h.text(), 'prompt> hello world');
});

test('a swipe keeps the first word when it arrives as an autocorrect replacement', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.input('prompt> hello world', 19, { inputType: 'insertReplacementText', data: 'hello world' });
  assert.equal(h.typed(), ' world');
  assert.equal(h.deleted(), false);
});

test('an insertion that would delete known text is reported with its evidence', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.input('prompt> world', 13, { inputType: 'insertText', data: 'world' });
  const reports = h.report.filter((entry) => entry.kind === 'native-insert-replaced');
  assert.equal(reports.length, 1, 'the overwrite signature must be recorded');
  assert.match(reports[0].detail, /removed=5/);
  assert.match(reports[0].detail, /inserted="world"/);
  assert.match(reports[0].detail, /shadow="hello"/);
  // Current behaviour mirrors the native field, which is why the record matters.
  assert.equal(h.sent.join('').includes('\x7f\x7f\x7f\x7f\x7f'), true);
});

test('a plain insertion never erases terminal text while the echo is pending', () => {
  // The shadow holds a padded screen line, as a TUI input box produces.
  const h = harness('                    ', 20, 'prompt> ');
  h.pane.mobilePredictionPending = true;
  h.pane.mobilePredictionConfirmed = false;
  h.input('prompt> this is', 'prompt> this is'.length, { inputType: 'insertText', data: ' is' });
  assert.equal(h.deleted(), false, 'no deletion may reach the terminal');
  assert.equal(h.sent.join('').endsWith('this is'), true, 'the typed text must arrive');
  assert.equal(h.pane.mobilePredictionText, `this is${' '.repeat(20)}`);
  assert.equal(h.pane.mobilePredictionCursor, 7);
});

test('typing while the echo is pending still sends the inserted text', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.pane.mobilePredictionPending = true;
  h.input('prompt> hello!', 'prompt> hello!'.length, { inputType: 'insertText', data: '!' });
  assert.deepEqual(h.sent, ['!']);
  assert.equal(h.pane.mobilePredictionText, 'hello!');
});

test('a confirmed shadow still mirrors a replacement that removes text', () => {
  const h = harness('abc def', 7, 'prompt> ');
  h.input('prompt> abc X', 'prompt> abc X'.length, { inputType: 'insertText', data: 'X' });
  assert.equal(h.deleted(), true, 'a confirmed shadow still mirrors the removal');
  assert.equal(h.sent.join('').includes('\x7f\x7f\x7f'), true);
});

test('an insertion against a diverged shadow delivers the typed text and erases nothing', () => {
  const h = harness('hello world', 11, 'prompt> ');
  h.pane.mobilePredictionPending = true;
  h.input('prompt> hello t', 'prompt> hello t'.length, { inputType: 'insertText', data: 't' });
  assert.equal(h.deleted(), false, 'a diverged insertion must never erase terminal text');
  assert.equal(h.sent.join('').endsWith('t'), true, 'the typed character must still arrive');
  assert.equal(h.pane.mobilePredictionText, 'hello tworld');
});

test('a diverged insertion with no typed text is dropped without erasing', () => {
  const h = harness('hello world', 11, 'prompt> ');
  h.pane.mobilePredictionPending = true;
  h.input('prompt> hello', 'prompt> hello'.length, { inputType: 'insertText', data: '' });
  assert.equal(h.deleted(), false);
  assert.deepEqual(h.sent, []);
  assert.equal(h.report.some((entry) => entry.detail.includes('diverged-insert')), true);
});
