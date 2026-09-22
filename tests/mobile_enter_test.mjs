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
const compositionFunctions = slice(
  '  function handleMobilePredictionCompositionStart(event)',
  '  function noteMobilePredictionTerminalData(pane, data)',
);
const terminalInputFunction = slice(
  '  function sendPaneTerminalInput(',
  '  function sendPaneResizes(',
);

function harness(text = '', cursor = text.length) {
  const sent = [];
  const reports = [];
  const prevented = [];
  const restored = [];
  let value = text;
  const helper = {
    get value() { return value; },
    set value(next) {
      value = next;
      this.selectionStart = this.selectionEnd = next.length;
    },
    selectionStart: cursor,
    selectionEnd: cursor,
    selectionDirection: 'none',
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
    },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    blur() {},
  };
  let rendered = text;
  const buffer = {
    baseY: 0,
    cursorY: 0,
    cursorX: cursor,
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
    mobilePredictionPrefix: '',
    mobilePredictionText: text,
    mobilePredictionCursor: cursor,
    mobilePredictionConfirmed: true,
    mobilePredictionPending: false,
  };
  const state = { control: false, alt: false, shift: false };
  const mode = { control: 'off', alt: 'off', shift: 'off' };
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
    mobileModifierState: state,
    mobileModifierMode: mode,
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
    MOBILE_COMPOSITION_STALL_MS: 5,
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
    reportClientIssue: (kind, detail) => { reports.push({ kind, detail }); },
    restoreMobilePredictionHelper: (candidate) => { restored.push(candidate); },
    showBrowserToast() {},
    clearTimeout,
    setTimeout,
    requestAnimationFrame: (callback) => callback(),
  });
  vm.runInContext(modifierFunctions, context);
  vm.runInContext(helperFunctions, context);
  vm.runInContext(keyHandlers, context);
  vm.runInContext(compositionFunctions, context);
  vm.runInContext(terminalInputFunction, context);
  const event = (fields = {}) => ({
    target: helper,
    isComposing: false,
    cancelable: true,
    preventDefault() { prevented.push(this.inputType || this.key); },
    stopImmediatePropagation() {},
    ...fields,
  });
  return {
    pane,
    helper,
    context,
    state,
    mode,
    sent,
    reports,
    prevented,
    restored,
    render(next, at = next.length) { rendered = next; buffer.cursorX = at; },
    input(next, at = next.length, fields = {}) {
      helper.value = next;
      helper.setSelectionRange(at, at);
      context.handleMobileTextInput(pane, event({ inputType: 'insertText', ...fields }));
    },
    beforeInput(fields = {}) {
      return event({ inputType: 'insertText', ...fields });
    },
    keydownReturn() {
      return event({ key: 'Enter', keyCode: 13 });
    },
  };
}

test('Return is sent once and leaves no newline in the helper', () => {
  const h = harness('ls', 2);
  h.context.handleMobileTerminalKeyDown(h.keydownReturn());
  assert.deepEqual(h.sent, ['\r']);
  assert.equal(h.helper.value, '', 'the finished terminal line must leave the helper');
  assert.equal(h.pane.mobilePredictionText, '');
});

test('the native line break that follows Return is suppressed', () => {
  const h = harness('ls', 2);
  h.context.handleMobileTerminalKeyDown(h.keydownReturn());
  const before = h.beforeInput({ inputType: 'insertLineBreak' });
  h.context.handleMobileTerminalBeforeInput(before);
  assert.deepEqual(h.sent, ['\r'], 'the line break must not send a second return');
  assert.equal(h.prevented.includes('insertLineBreak'), true, 'the native newline must be cancelled');
});

test('the first character typed after Return is delivered', () => {
  const h = harness('ls', 2);
  h.context.handleMobileTerminalKeyDown(h.keydownReturn());
  h.input('c', 1);
  assert.deepEqual(h.sent, ['\r', 'c']);
});

test('a line break that reaches the helper anyway keeps the next character', () => {
  const h = harness('ls', 2);
  h.input('c\n', 2, { inputType: 'insertLineBreak' });
  h.input('c\nd', 3);
  assert.equal(h.sent.includes('d'), true, 'a stray control character must not swallow the edit');
  assert.equal(h.sent.some((entry) => entry.includes('\n')), false);
});

test('Shift once converts Return and clears the one-shot mode', () => {
  const h = harness('ls', 2);
  h.state.shift = true;
  h.mode.shift = 'once';
  h.context.handleMobileTerminalKeyDown(h.keydownReturn());
  assert.deepEqual(h.sent, [terminalDataForModifiedEnter({ shift: true })]);
  assert.equal(h.mode.shift, 'off');
  assert.equal(h.state.shift, false);
});

test('Control once converts Return through the terminal data path and clears', () => {
  const h = harness('ls', 2);
  h.state.control = true;
  h.mode.control = 'once';
  h.context.sendPaneTerminalInput(h.pane, { clearSelection() {} }, '\r');
  assert.deepEqual(h.sent, [terminalDataForModifiedEnter({ control: true })]);
  assert.equal(h.mode.control, 'off');
});

test('unchanged terminal data keeps a one-shot modifier armed', () => {
  const h = harness('ls', 2);
  h.state.alt = true;
  h.mode.alt = 'once';
  h.context.sendPaneTerminalInput(h.pane, { clearSelection() {} }, '');
  assert.equal(h.mode.alt, 'once');
  assert.deepEqual(h.sent, ['']);
});

test('a held modifier survives the input it changed', () => {
  const h = harness('ls', 2);
  h.state.shift = true;
  h.mode.shift = 'hold';
  h.context.handleMobileTerminalKeyDown(h.keydownReturn());
  assert.equal(h.mode.shift, 'hold');
  assert.deepEqual(h.sent, [terminalDataForModifiedEnter({ shift: true })]);
});

test('a composition that never ends is committed when the keyboard goes quiet', async () => {
  const h = harness('hello', 5);
  h.context.handleMobilePredictionCompositionStart(h.beforeInput({ inputType: 'insertCompositionText' }));
  h.helper.value = 'helloX';
  h.helper.setSelectionRange('helloX'.length, 'helloX'.length);
  h.context.handleMobileTextInput(h.pane, h.beforeInput({ inputType: 'insertCompositionText', data: 'X' }));
  assert.deepEqual(h.sent, [], 'composition text waits for the commit');
  assert.equal(h.reports.some((entry) => entry.kind === 'input-swallowed'), true);
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.deepEqual(h.sent, ['X'], 'a stalled composition must still deliver the text');
  assert.equal(h.pane.mobilePredictionComposition, undefined);
});

test('dictation revisions are held until the hypothesis settles', async () => {
  const h = harness('', 0);
  h.input('se', 2, { inputType: 'insertText', data: 'se' });
  assert.deepEqual(h.sent, ['se'], 'the first word types like normal text');
  h.input('seems dictation', 15, { inputType: 'insertText', data: 'seems dictation' });
  h.input('seems dictation is broken', 25, { inputType: 'insertText', data: 'seems dictation is broken' });
  assert.deepEqual(h.sent, ['se'], 'provisional hypotheses must not be typed live');
  await new Promise((resolve) => setTimeout(resolve, 40));
  assert.equal(h.pane.mobilePredictionText, 'seems dictation is broken');
  assert.equal(h.pane.mobilePredictionCursor, 25);
  assert.equal(h.sent.length, 2, 'the settled sentence commits in one edit');
  assert.equal(h.sent[1].includes('ems dictation is broken'), true);
});
