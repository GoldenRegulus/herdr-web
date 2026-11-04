import assert from 'node:assert/strict';
import test from 'node:test';
import { rowContinuations } from '../herdr_web/static/pane-text.js';

const pad = (rows, cols) => rows.map((row) => row.padEnd(cols));

test('rows the terminal wrapped go on, real line breaks do not', () => {
  // As Herdr copies a long bash line, then two short ones.
  const rows = pad(['word0 word1 wor', 'd2 end', 'hard one', 'hard two'], 15);
  assert.deepEqual(
    rowContinuations(rows, 'word0 word1 word2 end\nhard one\nhard two'),
    [true, false, false],
  );
});

test('a full row that ends its line is a break, not a wrap', () => {
  const rows = pad(['exactly fifteen', 'next line'], 15);
  assert.deepEqual(rowContinuations(rows, 'exactly fifteen\nnext line'), [false]);
});

test('a wrap that fell on a space keeps the space', () => {
  const rows = pad(['aaaa bbbb ', 'cccc'], 10);
  assert.deepEqual(rowContinuations(rows, 'aaaa bbbb cccc'), [true]);
});

test('a program that breaks its own lines has real breaks', () => {
  // An agent wraps a URL itself, with a margin on each row: no terminal can
  // join these, and Herdr does not.
  const rows = pad([' https://e.com/a/very/lo', ' ng/path ok'], 25);
  assert.deepEqual(rowContinuations(rows, ' https://e.com/a/very/lo\n ng/path ok'), [false]);
});

test('blank rows and blank rows past the last line are breaks', () => {
  const rows = pad(['one', '', 'two', '', ''], 10);
  assert.deepEqual(rowContinuations(rows, 'one\n\ntwo'), [false, false, false, false]);
});

test('text that does not match the rows gives no answer', () => {
  const rows = pad(['one', 'two'], 10);
  assert.equal(rowContinuations(rows, 'one\nchanged'), undefined);
  assert.equal(rowContinuations(rows, 'one\ntwo\nthree'), undefined, 'more text than rows');
});
