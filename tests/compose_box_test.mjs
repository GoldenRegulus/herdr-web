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

test('focus puts the line back into the invisible box', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.helper.value = '';
  h.helper.setSelectionRange(0, 0);
  h.context.prepareMobilePredictionFocus(h.pane);
  assert.equal(h.helper.value, 'prompt> hello', 'the box mirrors the terminal line');
  assert.equal(
    h.helper.selectionStart, 'prompt> hello'.length,
    'with the caret at the end of the composed text',
  );
});

test('a deletion is an instruction and never waits for the settle', () => {
  const h = harness('abcdef', 6, 'prompt> ');
  h.type('prompt> abcd', 12);
  assert.equal(h.sent.join(''), '\x7f\x7f', 'both deletes go out at once');
  assert.equal(h.pane.mobilePredictionText, 'abcd');
});

test('a stream of larger edits cannot starve the settle deadline', async () => {
  const h = harness('', 0, 'prompt> ');
  let value = 'prompt> ';
  for (let index = 0; index < 8; index += 1) {
    value += `w${index} `;
    h.type(value, value.length);
    await settle(60);
  }
  assert.ok(h.sent.length > 0, 'the edits flush while the stream continues');
});

test('a swipe never counts as a tap', () => {
  const h = harness('', 0);
  assert.equal(h.context.isMobileTapGesture(0, 0, 80), true, 'a short touch is a tap');
  assert.equal(h.context.isMobileTapGesture(30, 4, 80), false, 'a drag is not');
  assert.equal(h.context.isMobileTapGesture(2, 2, 2000), false, 'and neither is a hold');
});

test('multi-line text is never replaced by one row', async () => {
  const h = harness('line1\nline2', 11, 'prompt> ');
  h.render('prompt> program row', 'prompt> program row'.length);
  h.frame();
  h.frame();
  assert.equal(
    h.pane.mobilePredictionText, 'line1\nline2',
    'the multi-line text survives a program row that repeated',
  );
});

test('a write keeps its moment through a confirming frame', async () => {
  const h = harness('o', 1, 'prompt> ');
  h.type('prompt> ', 8);
  assert.equal(h.pane.mobilePredictionText, '', 'the delete leaves nothing owned');
  // A stale frame that still shows the deleted text must not end the
  // write's moment: nothing may adopt it back.
  h.render('prompt> o', 9);
  h.frame();
  assert.ok(h.pane.composeWritten, 'the write in flight keeps its moment');
});

test('a stale caret can never lock the box', () => {
  const h = harness('ab', 2, 'prompt> ');
  h.pane.mobilePredictionCursor = 99;
  h.type('prompt> abc', 11);
  assert.equal(h.sent.join(''), 'c', 'typing goes through even from a caret the model lost');
});

function copyTerminal(rows, cols) {
  const lines = rows.map(({ text, wrapped = false }) => ({
    isWrapped: wrapped,
    getCell: (column) => {
      const padded = text.padEnd(cols);
      return column < cols ? { getChars: () => padded[column], getWidth: () => 1 } : undefined;
    },
  }));
  return { cols, buffer: { active: { getLine: (row) => lines[row] } } };
}

test('copy joins a wrapped row without a break and a newline with one', () => {
  const h = harness('', 0);
  const terminal = copyTerminal([
    { text: 'prompt> one' }, { text: 'two', wrapped: true }, { text: 'next' },
  ], 20);
  assert.equal(
    h.context.snapshotCopyText(terminal, 0, ['prompt> one', 'two', 'next']),
    'prompt> onetwo\nnext',
  );
});

test('copy joins a full row that Herdr drew without a wrap marker', () => {
  const h = harness('', 0);
  // Herdr positions every row itself and pads each one with spaces.
  const terminal = copyTerminal([
    { text: 'word0 word1 word' }, { text: '7 word8' }, { text: 'hard one' }, { text: 'hard two' },
  ], 16);
  assert.equal(
    h.context.snapshotCopyText(terminal, 0, [
      'word0 word1 word', '7 word8         ', 'hard one        ', 'hard two        ',
    ]),
    'word0 word1 word7 word8\nhard one\nhard two',
    'no break inside a wrapped word, and no padding before a newline',
  );
});

test('copy keeps the space of a wrap that fell on a word break', () => {
  const h = harness('', 0);
  const terminal = copyTerminal([{ text: 'aaaa bbbb ' }, { text: 'cccc' }, { text: 'short' }, { text: 'end' }], 10);
  assert.equal(
    h.context.snapshotCopyText(terminal, 0, ['aaaa bbbb ', 'cccc      ', 'short     ', 'end']),
    'aaaa bbbb cccc\nshort\nend',
  );
});

test('a delete at the row start goes to the terminal', () => {
  const h = harness('line2', 5);
  assert.equal(
    h.context.mobileDeleteAtBoxStart(h.helper, h.pane), false,
    'with text before the caret the native delete applies',
  );
  h.caret(0);
  assert.equal(
    h.context.mobileDeleteAtBoxStart(h.helper, h.pane), true,
    'at the row start the delete goes to the terminal',
  );
  h.pane.nativeComposing = true;
  assert.equal(
    h.context.mobileDeleteAtBoxStart(h.helper, h.pane), false,
    'and never while the keyboard composes',
  );
});

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

test('a program that changes its own line is left alone with the draft', () => {
  const h = harness('hello', 5, 'prompt> ');
  h.render('prompt> shell replaced this', 'prompt> shell replaced this'.length);
  h.frame();
  h.render('prompt> shell replaced this', 'prompt> shell replaced this'.length);
  h.frame();
  assert.equal(h.helper.value, 'prompt> hello', 'the draft the user typed stays');
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

test('a redraw fragment never steals the draft', async () => {
  const h = harness('hello', 5, 'prompt> ');
  h.render('s time, and the two skip       buttons.', 40);
  h.frame();
  h.render('s time, and the two skip       buttons.', 40);
  h.frame();
  assert.equal(h.helper.value, 'prompt> hello', 'padded screen rows never take the draft');
  assert.deepEqual(h.sent, [], 'and nothing corrects the program');
});

test('a settled line is taken when nothing is owned', async () => {
  const h = harness('', 0, 'prompt> ');
  h.render('prompt> next thing', 'prompt> next thing'.length);
  h.frame();
  h.frame();
  assert.equal(h.helper.value, 'prompt> next thing');
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
  const shadow = prediction.terminalTextAtCursor(terminal, { text: 'line-one\nline-two', cursor: 17 });
  assert.equal(shadow.text, 'prompt> line-one\nline-two', 'the rows are one editable text');
  assert.equal(shadow.cursor, 25);
  // deleting at the boundary between the two rows
  const next = shadow.text.slice(0, 15) + shadow.text.slice(16);
  const edit = prediction.terminalTextInputDelta(shadow.text, next, 16, 15);
  assert.equal(edit.data, '\x7f', 'the delete crosses the row boundary');
});

test('a control key sends text that is still settling before itself', () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> echo ', 13);
  h.type('prompt> echo quick', 18);
  assert.equal(h.sent.join(''), '', 'a predictive word waits for the settle');
  h.context.clearMobilePredictionState(h.pane, true);
  h.sent.push('\r');
  assert.deepEqual(h.sent, ['echo quick', '\r'], 'the word reaches the terminal ahead of Return');
  assert.equal(h.pane.composeTimer, undefined, 'and the settle is cancelled');
});

test('a reset model judges the next keystroke on its own', () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> echo ab', 15);
  h.context.clearMobilePredictionState(h.pane, true);
  h.sent.length = 0;
  h.pane.mobilePredictionPrefix = 'prompt> echo a';
  h.helper.value = 'prompt> echo a';
  h.type('prompt> echo aX', 15);
  assert.deepEqual(h.sent, ['X'], 'one typed character goes out at once');
});

test('a hyphen that iOS turned into a dash stays two hyphens', () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> ls -', 12);
  // The second hyphen: iOS replaces the first with an em dash.
  h.type('prompt> ls —', 12, { inputType: 'insertReplacementText' });
  assert.equal(h.helper.value, 'prompt> ls --', 'the box shows what was typed');
  assert.equal(h.sent.join(''), 'ls --', 'and the terminal receives two hyphens');
  h.type('prompt> ls --a', 14);
  assert.equal(h.sent.join(''), 'ls --a');
});

test('a dash picked on purpose is kept', () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> a', 9);
  h.type('prompt> a—', 10);
  assert.equal(h.sent.join(''), 'a—', 'inserting a dash replaces no hyphen');
  h.type('prompt> a—-', 11);
  h.type('prompt> a——', 11);
  assert.equal(h.helper.value, 'prompt> a—--', 'only the hyphen iOS replaced comes back');
});

test('a curly quote from the plain quote key goes out straight', () => {
  const h = harness('', 0, 'prompt> ');
  h.pane.lastTypedKey = { key: "'", at: Date.now() };
  h.type('prompt> ’', 9);
  assert.equal(h.sent.join(''), "'");
  assert.equal(h.helper.value, "prompt> '");
  h.pane.lastTypedKey = { key: '’', at: Date.now() };
  h.type("prompt> '’", 10);
  assert.equal(h.sent.join(''), "'’", 'a curly quote picked from the key variants is kept');
});

test('a dash that iOS sends as a delete and an insert stays two hyphens', () => {
  // The sequence an iPhone sent for a second hyphen, from its input log.
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> a', 9);
  h.pane.lastTypedKey = { key: '-', at: Date.now() };
  h.type('prompt> a-', 10);
  h.pane.lastTypedKey = { key: '-', at: Date.now() };
  h.type('prompt> a', 9, { inputType: 'deleteContentBackward' });
  // iOS reports the caret move between the two edits.
  h.caret(9);
  h.type('prompt> a—', 10);
  assert.equal(h.helper.value, 'prompt> a--', 'the box shows what was typed');
  assert.equal(h.sent.join(''), 'a-\x7f--', 'and the terminal ends with two hyphens');
  h.type('prompt> a--b', 12);
  assert.equal(h.sent.join(''), 'a-\x7f--b');
});

test('a dash picked after deleting a hyphen with backspace is kept', () => {
  const h = harness('', 0, 'prompt> ');
  h.type('prompt> a', 9);
  h.type('prompt> a-', 10);
  h.pane.lastTypedKey = { key: 'Backspace', at: Date.now() };
  h.type('prompt> a', 9, { inputType: 'deleteContentBackward' });
  h.type('prompt> a—', 10);
  assert.equal(h.sent.join(''), 'a-\x7f—', 'the deletion was not the hyphen key');
});

// Replays of an iPhone's input log. `typeEach` types like a keyboard: one
// input event per character.
function typeEach(h, prefix, text, from = '') {
  let value = from;
  for (const character of text) {
    value += character;
    h.type(prefix + value, prefix.length + value.length);
  }
}

test('a space that WebKit stores as a no-break space reaches the shell as a space', () => {
  const h = harness('', 0, 'prompt> ');
  typeEach(h, 'prompt> ', 'echo');
  h.type('prompt> echo ', 13);
  assert.equal(h.sent.join(''), 'echo ', 'a shell reads a no-break space as part of a word');
  // At the next key WebKit turns it back into a space: nothing to resend.
  h.type('prompt> echo a', 14);
  assert.equal(h.sent.join(''), 'echo a');
});

test('curly quotes typed on the quote keys go out straight, as an iPhone sends them', () => {
  const h = harness('', 0, 'prompt> ');
  typeEach(h, 'prompt> ', 'echo');
  h.type('prompt> echo ', 13);
  h.pane.lastTypedKey = { key: '"', at: Date.now() };
  h.type('prompt> echo “', 14);
  assert.equal(h.helper.value, 'prompt> echo "');
  h.type('prompt> echo "b', 15);
  h.pane.lastTypedKey = { key: '"', at: Date.now() };
  h.type('prompt> echo "b”', 16);
  assert.equal(h.sent.join(''), 'echo "b"');
});

test('three periods stay periods when iOS sends an ellipsis', () => {
  const h = harness('', 0, 'prompt> ');
  typeEach(h, 'prompt> ', 'echo ..');
  // The third period: the key, two deletions, then the ellipsis.
  h.pane.lastTypedKey = { key: '.', at: Date.now() };
  h.type('prompt> echo .', 14, { inputType: 'deleteContentBackward' });
  h.type('prompt> echo ', 13, { inputType: 'deleteContentBackward' });
  h.caret(13);
  h.type('prompt> echo …', 14);
  assert.equal(h.helper.value, 'prompt> echo ...', 'the box shows what was typed');
  assert.equal(h.sent.join(''), 'echo ..\x7f\x7f...', 'and the terminal ends with three periods');
});

test('a double space that iOS turns into a period stays two spaces', () => {
  for (const shape of ['two edits', 'one edit']) {
    const h = harness('', 0, 'prompt> ');
    typeEach(h, 'prompt> ', 'a');
    h.type('prompt> a ', 10);
    h.pane.lastTypedKey = { key: ' ', at: Date.now() };
    if (shape === 'two edits') h.type('prompt> a', 9, { inputType: 'deleteContentBackward' });
    h.type('prompt> a. ', 11);
    assert.equal(h.helper.value, 'prompt> a  ', `${shape}: the box shows what was typed`);
    h.type('prompt> a  b', 12);
    assert.equal(h.sent.join('').replace(/ \x7f/g, ''), 'a  b', `${shape}: the terminal ends with two spaces`);
  }
});

test('a period and space without the space key are left alone', () => {
  // Android reports no keys: this may be a space after a typed period.
  const h = harness('', 0, 'prompt> ');
  typeEach(h, 'prompt> ', 'a ');
  h.pane.lastTypedKey = { key: 'Unidentified', at: Date.now() };
  h.type('prompt> a. ', 11);
  assert.equal(h.helper.value, 'prompt> a. ');
  assert.ok(h.sent.join('').includes('.'), 'the period goes to the terminal');
});
