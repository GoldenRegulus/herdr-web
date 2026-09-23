import assert from 'node:assert/strict';
import test from 'node:test';
import * as prediction from '../herdr_web/static/mobile-prediction.js';

// The editable region. The compose box edits the text the user composed:
// rows joined by a wrap, and rows created by the line breaks it sent. It
// never reaches into rows the user did not write.

function rowsTerminal(rows, { row = 0, x = 0, wrapped = [] } = {}) {
  return {
    buffer: {
      active: {
        baseY: 0,
        cursorY: row,
        cursorX: x,
        getLine(index) {
          const text = rows[index];
          if (text === undefined) return undefined;
          return {
            isWrapped: wrapped.includes(index),
            translateToString: (trim, start = 0, end = text.length) => {
              const part = text.slice(start, end);
              return trim ? part.trimEnd() : part;
            },
          };
        },
      },
    },
  };
}

test('output below the cursor is never part of the shadow', () => {
  const terminal = rowsTerminal(
    ['prompt> typed', 'ls: output the user did not write'],
    { row: 0, x: 13 },
  );
  const shadow = prediction.terminalTextAtCursor(terminal);
  assert.equal(shadow.text, 'prompt> typed', 'the output row stays out');
  const next = shadow.text.slice(0, shadow.text.length - 1);
  const edit = prediction.terminalTextInputDelta(shadow.text, next, shadow.cursor, next.length);
  assert.equal(edit.data, '\x7f', 'the delete it implies is one character');
  assert.equal(edit.removed, 1, 'and never reaches the output row');
});

test('a line break the user composed grows the region across rows', () => {
  const terminal = rowsTerminal(['prompt> abc', 'def'], { row: 1, x: 3 });
  const none = prediction.terminalTextAtCursor(terminal);
  assert.equal(none.text, 'def', 'without a composed line break the row stands alone');
  const shadow = prediction.terminalTextAtCursor(terminal, { text: 'abc\ndef', cursor: 7 });
  assert.equal(shadow.text, 'prompt> abc\ndef', 'the composed break joins the rows with a break');
  assert.equal(shadow.cursor, 'prompt> abc\ndef'.length);
});

test('the prefix derivation knows the region from the composed text', () => {
  const terminal = rowsTerminal(['prompt> abc', 'def'], { row: 1, x: 3 });
  const prefix = prediction.terminalPredictionPrefix(terminal, 'abc\ndef', 7);
  assert.equal(prefix, 'prompt> ', 'the rows of the composed text are its own line');
});

test('a delete stays inside the region', () => {
  const terminal = rowsTerminal(
    ['prompt> abc', 'def', 'OUTPUT THE USER DID NOT WRITE'],
    { row: 1, x: 3 },
  );
  const shadow = prediction.terminalTextAtCursor(terminal, { text: 'abc\ndef', cursor: 7 });
  assert.equal(shadow.text, 'prompt> abc\ndef', 'the third row is outside the region');
  const next = shadow.text.slice(0, shadow.text.length - 3);
  const edit = prediction.terminalTextInputDelta(shadow.text, next, shadow.cursor, next.length);
  assert.equal(edit.data, '\x7f'.repeat(3), 'the delete removes exactly the owned text');
});

test('a wrapped row joins without a break and needs no justification', () => {
  const terminal = rowsTerminal(
    ['prompt> long-text-contin', 'uation'],
    { row: 1, x: 8, wrapped: [1] },
  );
  const shadow = prediction.terminalTextAtCursor(terminal);
  assert.equal(shadow.text, 'prompt> long-text-continuation', 'one logical line');
  const next = shadow.text.slice(0, 23) + shadow.text.slice(24);
  const edit = prediction.terminalTextInputDelta(shadow.text, next, 24, 23);
  assert.equal(edit.data, '\x7f', 'a delete crosses the wrap like any character');
});

test('a reflow that rewraps rows leaves the composed text unchanged', () => {
  const wide = rowsTerminal(['prompt> abcdefghij'], { row: 0, x: 15 });
  const narrow = rowsTerminal(['prompt> abcdef', 'ghij'], { row: 1, x: 1, wrapped: [1] });
  const before = prediction.terminalTextAtCursor(wide);
  const after = prediction.terminalTextAtCursor(narrow);
  assert.equal(after.text, before.text, 'the joined text is the same');
  assert.equal(after.cursor, before.cursor, 'and the caret is in the same place');
  const edit = prediction.terminalTextInputDelta(before.text, after.text, before.cursor, after.cursor);
  assert.equal(edit.data, '', 'so the reflow sends nothing');
  assert.equal(edit.removed, 0);
  assert.equal(edit.inserted, '');
});
