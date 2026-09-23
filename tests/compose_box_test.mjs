import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as prediction from '../herdr_web/static/mobile-prediction.js';

// The compose box behaviour. The invisible textarea holds the text being
// composed, the terminal line is what it mirrors, and the terminal wins every
// tie: a program that rewrites, ignores, or has weird input is followed or
// left alone, never fought.
const app = readFileSync(new URL('../herdr_web/static/app.js', import.meta.url), 'utf8');
const core = app.slice(
  app.indexOf('  function paneKeyboardHelper(pane)'),
  app.indexOf('  function noteMobilePredictionTerminalData(pane, data)'),
);

const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function harness(text = '', cursor = text.length, prefix = '') {
  const sent = [];
  let value = prefix + text;
  let rendered = prefix + text;
  const helper = {
    get value() { return value; },
    set value(next) { value = next; this.selectionStart = this.selectionEnd = next.length; },
    selectionStart: prefix.length + cursor,
    selectionEnd: prefix.length + cursor,
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    blur() {},
  };
  const buffer = {
    baseY: 0,
    cursorY: 0,
    cursorX: prefix.length + cursor,
    getLine(row) {
      return row === 0
        ? { isWrapped: false, translateToString: (_trim, start = 0, end = rendered.length) => rendered.slice(start, end) }
        : undefined;
    },
  };
  const pane = {
    mode: 'control',
    closed: false,
    streamId: 'w1:p1',
    terminal: { textarea: helper, modes: {}, buffer: { active: buffer }, clearSelection() {} },
    mobilePredictionPrefix: prefix,
    mobilePredictionText: text,
    mobilePredictionCursor: cursor,
  };
  const context = vm.createContext({
    ...prediction,
    pane,
    helper,
    document: {
      activeElement: helper,
      getSelection: () => '',
      documentElement: { dataset: { mobileKeyboard: 'open' } },
    },
    mobileQuery: { matches: true },
    iosKeyboard: true,
    androidKeyboard: false,
    nativeKeyboardInput: true,
    mobileKeyboardLocked: false,
    mobileModifierState: { control: false, alt: false, shift: false },
    mobileModifierMode: { control: 'off', alt: 'off', shift: 'off' },
    mobileModifiersArmed: () => Object.values(context.mobileModifierState).some(Boolean),
    terminal: { focus() {} },
    selectedPaneTerminal: () => pane,
    focusPaneKeyboard() {},
    paneForKeyboardTarget: (target) => (target === helper ? pane : undefined),
    paneAcceptsInput: (candidate) => candidate.mode === 'control' && !candidate.closed,
    setActivePane: () => true,
    sendInput: (data) => { sent.push(data); },
    sendMobilePaneKeyboardData: (candidate, data) => {
      if (candidate.mode !== 'control' || candidate.closed) return false;
      sent.push(data);
      return true;
    },
    applyMobileModifiers: (data) => data,
    consumeMobileModifiers() {},
    setMobilePredictionAttributes() {},
    showBrowserToast() {},
    reportClientIssue() {},
    performance: { now: () => Date.now() },
    clearTimeout,
    setTimeout,
  });
  vm.runInContext(core, context);
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
    type(value, at = value.length, fields = {}) {
      helper.value = value;
      helper.setSelectionRange(at, at);
      context.handleMobileTextInput(pane, event({ inputType: 'insertText', ...fields }));
    },
    render(next, at = next.length) { rendered = next; buffer.cursorX = at; },
    frame() { context.syncMobilePredictionFromTerminal(pane); },
    caret(at) {
      helper.setSelectionRange(at, at);
      context.handleMobileCaretSelection();
    },
  };
}

test('one character of typing goes out at once', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.type('prompt> hello!');
  assert.deepEqual(h.sent, ['!']);
  assert.equal(h.pane.mobilePredictionText, 'hello!');
});

test('a swipe word or a hypothesis settles into one edit', async () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> seems fine');
  assert.deepEqual(h.sent, [], 'the terminal never churns through recognitions');
  await settle(320);
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].includes('seems fine'), true);
  assert.equal(h.pane.mobilePredictionText, 'seems fine');
});

test('typing during the settle sends everything at once', async () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> seems fine');
  h.type('prompt> seems fineq');
  assert.equal(h.sent.length, 1, 'the typed character is instant and carries the rest');
  assert.equal(h.sent[0].includes('seems fineq'), true);
  await settle(320);
  assert.equal(h.sent.length, 1, 'nothing is sent twice');
});

test('a caret move sends cursor movement and no text', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.caret('prompt> '.length + 2);
  assert.deepEqual(h.sent, ['\x1b[D'.repeat(3)]);
  assert.equal(h.pane.mobilePredictionCursor, 2);
  assert.equal(h.pane.mobilePredictionText, 'hello', 'a caret move is not an edit');
});

test('backspace deletes exactly one character', () => {
  const h = harness('abc', 3, 'prompt> ');
  h.type('prompt> ab');
  assert.deepEqual(h.sent, ['\x7f']);
  assert.equal(h.pane.mobilePredictionText, 'ab');
});

test('a partial echo does not swallow what was typed', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.type('prompt> hello!');
  assert.deepEqual(h.sent, ['!']);
  h.render('prompt> hell', 'prompt> hell'.length);
  h.frame();
  assert.equal(h.helper.value, 'prompt> hello!', 'the box keeps the text while the echo is in flight');
  h.type('prompt> hello!!');
  assert.deepEqual(h.sent, ['!', '!'], 'the next write rebases on the line as observed');
});

test('a program that changes its own line is followed, not fought', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.render('prompt> shell replaced this', 'prompt> shell replaced this'.length);
  h.frame();
  h.render('prompt> shell replaced this', 'prompt> shell replaced this'.length);
  h.frame();
  assert.equal(h.pane.mobilePredictionText, 'prompt> shell replaced this');
  assert.equal(h.helper.value, 'prompt> shell replaced this', 'the box adopts what the program shows');
  assert.deepEqual(h.sent, [], 'no edit corrects the program');
});

test('a composition that never ends does not block typing', async () => {
  const h = harness('', 0, 'prompt> ');
  h.context.handleMobilePredictionCompositionStart({
    target: h.helper,
    stopImmediatePropagation() {},
  });
  h.type('prompt> composed text', 'prompt> composed text'.length, { inputType: 'insertCompositionText' });
  assert.deepEqual(h.sent, [], 'the system owns the box while it composes');
  await settle(1100);
  assert.equal(h.pane.nativeComposing, undefined);
  h.type('prompt> composed textq');
  assert.equal(h.sent.length, 1, 'typing works again once the keyboard goes quiet');
  assert.equal(h.sent[0].includes('composed textq'), true);
});

test('a control character in the box never eats the next character', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.type('prompt> hello\x00q', 'prompt> hello\x00q'.length);
  assert.equal(h.helper.value.includes('\x00'), false, 'a control character is not text');
  assert.equal(h.pane.mobilePredictionText, 'helloq');
  assert.deepEqual(h.sent, ['q'], 'the next character is not eaten');
});

test('a paste keeps its structured path', () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> pasted', 'prompt> pasted'.length, { inputType: 'insertFromPaste' });
  assert.deepEqual(h.sent, [], 'the compose box leaves paste to the paste path');
});

test('a reflow that redraws the line never throws away typed text', () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> draft text', 'prompt> draft text'.length);
  h.frame();
  // a program mid-redraw shows a different line in every frame
  h.render('prompt> draft', 'prompt> draft'.length);
  h.frame();
  h.render('prompt> draft tex', 'prompt> draft tex'.length);
  h.frame();
  assert.equal(h.helper.value, 'prompt> draft text', 'the box keeps what the user typed');
  assert.deepEqual(h.sent, [], 'no edit fights the redraw');
});

test('a line the program really changed is adopted once it settles', async () => {
  const h = harness('hello', 5, 'prompt> ');
  h.render('prompt> program moved on', 'prompt> program moved on'.length);
  h.frame();
  h.frame();
  assert.equal(h.helper.value, 'prompt> program moved on', 'a settled line is taken');
});

test('a delete crosses the line break of multi-line text', async () => {
  const h = harness('line1\nline2', 11, 'prompt> ');
  h.type('prompt> line1\nline', 'prompt> line1\nline'.length);
  assert.deepEqual(h.sent, ['\x7f'], 'the delete crosses into the second line');
  assert.equal(h.pane.mobilePredictionText, 'line1\nline');
  h.type('prompt> line1', 'prompt> line1'.length);
  await settle(320);
  assert.equal(h.sent[1], '\x7f'.repeat(5), 'the line break itself is deleted too');
  assert.equal(h.pane.mobilePredictionText, 'line1');
});

test('the shadow crosses a row boundary and a delete crosses with it', () => {
  const rows = ['prompt> line-one', 'line-two'];
  const terminal = {
    buffer: {
      active: {
        baseY: 0,
        cursorY: 1,
        cursorX: 8,
        getLine(row) {
          const text = rows[row];
          return text === undefined ? undefined : {
            isWrapped: false,
            translateToString: (trim, start = 0, end = text.length) => {
              const part = text.slice(start, end);
              return trim ? part.trimEnd() : part;
            },
          };
        },
      },
    },
  };
  const shadow = prediction.terminalTextAtCursor(terminal);
  assert.equal(shadow.text, 'prompt> line-oneline-two', 'the rows are one editable text');
  assert.equal(shadow.cursor, 24);
  // deleting at the boundary between the two rows
  const next = shadow.text.slice(0, 15) + shadow.text.slice(16);
  const edit = prediction.terminalTextInputDelta(shadow.text, next, 16, 15);
  assert.equal(edit.data, '\x7f', 'the delete crosses the row boundary');
});
