import assert from 'node:assert/strict';
import test from 'node:test';
import {
  MOBILE_PREDICTION_TEXT_LIMIT,
  terminalCaretInput,
  terminalHasEditableSuffix,
  terminalHasEditableText,
  terminalPredictionReplacement,
  terminalTextInputDelta,
} from '../herdr_web/static/mobile-prediction.js';

const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
const parts = (text) => [...segmenter.segment(text)].map((part) => part.segment);
const prefix = 'unknown prefix|';
const suffix = '|unknown suffix';

function applyTerminalData(state, data) {
  while (data) {
    const arrow = /^\x1b[\[O]([CD])/.exec(data);
    if (arrow) {
      state.cursor += arrow[1] === 'D'
        ? -(parts(state.text.slice(0, state.cursor)).at(-1)?.length || 0)
        : (parts(state.text.slice(state.cursor))[0]?.length || 0);
      data = data.slice(arrow[0].length);
    } else if (data[0] === '\x7f') {
      const length = parts(state.text.slice(0, state.cursor)).at(-1)?.length || 0;
      state.text = state.text.slice(0, state.cursor - length) + state.text.slice(state.cursor);
      state.cursor -= length;
      data = data.slice(1);
    } else {
      const inserted = /^[^\x1b\x7f]+/u.exec(data)?.[0];
      assert.ok(inserted, 'unexpected terminal control');
      state.text = state.text.slice(0, state.cursor) + inserted + state.text.slice(state.cursor);
      state.cursor += inserted.length;
      data = data.slice(inserted.length);
    }
    assert.ok(state.cursor >= prefix.length, 'movement crossed the unknown prefix');
    assert.ok(state.cursor <= state.text.length - suffix.length, 'movement crossed the unknown suffix');
    assert.ok(state.text.startsWith(prefix));
    assert.ok(state.text.endsWith(suffix));
  }
  return state;
}

function checkEdit(previous, next, from = previous.length, to = next.length, appMode = false) {
  const edit = terminalTextInputDelta(previous, next, from, to, appMode);
  assert.ok(edit);
  const state = applyTerminalData({ text: prefix + previous + suffix, cursor: prefix.length + from }, edit.data);
  assert.equal(state.text, prefix + next + suffix);
  assert.equal(state.cursor, prefix.length + to);
  return edit;
}

function terminalWithRows(rows, cursorX = rows.at(-1).length, cursorY = rows.length - 1) {
  return { buffer: { active: {
    baseY: 0, cursorX, cursorY,
    getLine: (row) => rows[row] === undefined ? undefined : {
      isWrapped: false,
      translateToString: (_trimRight, start = 0, end = rows[row].length) => rows[row].slice(start, end),
    },
  } } };
}

test('native input preserves unchanged text on both sides of the edit', () => {
  assert.deepEqual(checkEdit('th', 'they are being'), {
    data: 'ey are being', removed: 0, inserted: 'ey are being',
  });
  const edit = checkEdit('they are being', "they're being");
  assert.equal(edit.removed, 2);
  assert.equal(edit.inserted, "'");
  for (const [before, after, from, to] of [
    ['hello', 'Xhello', 0, 1],
    ['hello', 'heXllo', 2, 3],
    ['hello', 'helloX', 5, 6],
    ['abc def', 'abc NEWdef', 7, 7],
    ['abc def', 'abc XYZ', 1, 7],
    ['abc def', 'abcdef', 4, 3],
    ['abcdef', '', 2, 0],
    ['', 'new', 0, 0],
  ]) checkEdit(before, after, from, to);
});

test('caret-only changes and duplicate events preserve the terminal text', () => {
  assert.equal(checkEdit('abcdef', 'abcdef', 6, 2).data, '\x1b[D'.repeat(4));
  assert.equal(checkEdit('abcdef', 'abcdef', 2, 5).data, '\x1b[C'.repeat(3));
  assert.equal(checkEdit('abcdef', 'abcdef', 2, 2).data, '');
  assert.equal(terminalCaretInput('abc', 3, 0, true), '\x1bOD'.repeat(3));
  assert.equal(terminalCaretInput('abc', 0, 3, true), '\x1bOC'.repeat(3));
  checkEdit('abcdef', 'abXcdef', 6, 3, true);
  checkEdit('abcdef', 'abXcdef', 2, 3);
});

test('dictation revisions and unchanged duplicates produce one final hypothesis', () => {
  const hypotheses = ['th', 'they', 'they are being', 'they are being unnecessary',
    "they're being unnecessary code", "they're being unnecessary code"];
  let previous = '';
  for (const hypothesis of hypotheses) {
    checkEdit(previous, hypothesis);
    previous = hypothesis;
  }
});

test('caret movement and replacement use grapheme boundaries, not UTF-16 units or cell width', () => {
  const text = 'a👍🏽e\u0301日本';
  assert.equal(terminalCaretInput(text, text.length, 1), '\x1b[D'.repeat(4));
  assert.equal(terminalCaretInput(text, 1, 5), '\x1b[C');
  assert.equal(terminalCaretInput(text, 2, 5), undefined);
  assert.equal(terminalCaretInput(text, 5, 6), undefined);
  checkEdit('go 👍🏽 now', 'go 👩‍💻 now', 7, 8);
  checkEdit('cafe', 'cafe\u0301', 4, 5);
  checkEdit('日本語', '日X本語', 1, 2);
});

test('all small bounded text edits preserve unknown terminal text and final caret', () => {
  const values = ['', 'a', 'ab', 'aaa', 'a b', '日本', 'e\u0301👍🏽'];
  const offsets = (text) => [0, ...[...segmenter.segment(text)].map((part) => part.index + part.segment.length)];
  for (const before of values) for (const after of values) {
    for (const from of offsets(before)) for (const to of offsets(after)) {
      checkEdit(before, after, from, to);
    }
  }
});

test('prediction replaces only a bounded selected owned range', () => {
  for (const [text, start, end, replacement, cursor] of [
    ['git chekout', 4, 11, 'checkout', 11],
    ['the old word', 4, 7, 'new', 2],
    ['ab👍🏽cd', 2, 6, 'X', 0],
  ]) {
    const result = terminalPredictionReplacement({ text, selectionStart: start,
      selectionEnd: end, replacement, cursor, editable: true });
    assert.ok(result);
    const state = applyTerminalData({ text: prefix + text + suffix, cursor: prefix.length + cursor }, result.data);
    assert.equal(state.text, prefix + result.text + suffix);
    assert.equal(state.cursor, prefix.length + result.cursor);
  }
  assert.equal(terminalPredictionReplacement({ text: 'abc', selectionStart: 0,
    selectionEnd: 2, replacement: 'X', editable: false }), undefined);
  assert.equal(terminalPredictionReplacement({ text: '👍🏽', selectionStart: 1,
    selectionEnd: 4, replacement: 'X', editable: true }), undefined);
});

test('confirmation requires complete owned text across physical rows and the cursor', () => {
  const text = 'git chekout';
  assert.equal(terminalHasEditableSuffix(terminalWithRows(['prompt> git ch', 'ekout']), text), true);
  assert.equal(terminalHasEditableText(terminalWithRows(['prompt> git ch', 'ekout'], 10, 0), text), true);
  assert.equal(terminalHasEditableText(terminalWithRows(['prompt> abc ', 'def'], 9, 0), 'abc def'), true);
  assert.equal(terminalHasEditableText(terminalWithRows(['prompt> abcdef'], 8), 'abcdef'), true);
  assert.equal(terminalHasEditableText(terminalWithRows(['prompt> abcdef'], 14), 'abcdef'), true);
  assert.equal(terminalHasEditableText(terminalWithRows(['prompt> abcdef'], 2), 'abcdef'), false);
  assert.equal(terminalHasEditableSuffix(terminalWithRows(['output: chekout']), text), false);
  assert.equal(terminalHasEditableText(terminalWithRows(['eted']), 'deploy completed'), false);
  assert.equal(terminalHasEditableText(terminalWithRows(['unrelated output']), text), false);
});

test('invalid text, caret offsets, and oversized replacements produce no input', () => {
  for (const value of [null, undefined, 42, '\n', 'a\x1bb', 'x'.repeat(MOBILE_PREDICTION_TEXT_LIMIT + 1)]) {
    assert.equal(terminalTextInputDelta('abc', value), undefined);
    assert.equal(terminalCaretInput(value, 0, 0), undefined);
  }
  for (const cursor of [-1, 4, 0.5, NaN]) {
    assert.equal(terminalTextInputDelta('abc', 'abc', cursor, 0), undefined);
    assert.equal(terminalCaretInput('abc', 0, cursor), undefined);
  }
  assert.equal(terminalPredictionReplacement({ text: 'abcd', selectionStart: 1,
    selectionEnd: 2, replacement: 'x'.repeat(1024), editable: true }), undefined);
});
