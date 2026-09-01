import assert from 'node:assert/strict';
import {
  InputByteBuffer,
  isDisposableMouseMotion,
  normalizeTerminalPasteText,
  terminalDataForBeforeInput,
  terminalDataForModifiedEnter,
  terminalDataForNavigationKey,
  terminalDataForRepeatedMobileBackspace,
} from '../herdr_web/static/input-buffer.js';

const encoder = new TextEncoder();
const decoder = new TextDecoder();

{
  const buffer = new InputByteBuffer(encoder, 8);
  buffer.append('ab');
  buffer.append('é🙂');
  assert.equal(decoder.decode(buffer.peek(buffer.length)), 'abé🙂');
  assert.equal(buffer.enqueuedBytes, encoder.encode('abé🙂').length);
}

{
  const buffer = new InputByteBuffer(encoder, 8);
  buffer.append('abcdef');
  buffer.consume(5);
  buffer.append('12345');
  assert.equal(decoder.decode(buffer.peek(buffer.length)), 'f12345');
  assert.equal(buffer.consumedBytes, 5);
}

{
  const buffer = new InputByteBuffer(encoder, 4);
  const input = 'λ'.repeat(10_000);
  buffer.append(input);
  assert.equal(decoder.decode(buffer.peek(buffer.length)), input);
  buffer.consume(buffer.length);
  assert.equal(buffer.length, 0);
  buffer.clear();
  assert.equal(buffer.enqueuedBytes, 0);
  assert.equal(buffer.consumedBytes, 0);
}

{
  const buffer = new InputByteBuffer(encoder, 16);
  buffer.append('x'.repeat(100_000));
  assert.ok(buffer.bytes.length > 64 * 1024);
  buffer.consume(buffer.length);
  assert.equal(buffer.bytes.length, 16);
}

{
  const buffer = new InputByteBuffer(encoder, 4, 8);
  buffer.append('12345678');
  assert.throws(() => buffer.append('9'), /queue is full/);
  assert.equal(decoder.decode(buffer.peek(buffer.length)), '12345678');
}

assert.throws(() => {
  const buffer = new InputByteBuffer(encoder);
  buffer.consume(1);
}, RangeError);

assert.equal(isDisposableMouseMotion('\x1b[<35;130;30M'), true);
assert.equal(isDisposableMouseMotion('\x1b[<32;10;20M'), true);
assert.equal(isDisposableMouseMotion(`\x1b[M${String.fromCharCode(67, 42, 52)}`), true);
assert.equal(isDisposableMouseMotion(`\x1b[M${String.fromCharCode(67, 42, 52)}paste`), false);
assert.equal(isDisposableMouseMotion('\x1b[<0;10;20M'), false);
assert.equal(isDisposableMouseMotion('\x1b[<0;10;20m'), false);
assert.equal(isDisposableMouseMotion('\x1b[<64;10;20M'), false);
assert.equal(isDisposableMouseMotion('q'), false);
assert.equal(isDisposableMouseMotion('\x1b[<35;10;20Mmore'), false);

const mobileEnterModifiers = [
  [{ shift: true }, '\x1b[13;2u'],
  [{ alt: true }, '\x1b[13;3u'],
  [{ shift: true, alt: true }, '\x1b[13;4u'],
  [{ control: true }, '\x1b[13;5u'],
  [{ shift: true, control: true }, '\x1b[13;6u'],
  [{ alt: true, control: true }, '\x1b[13;7u'],
  [{ shift: true, alt: true, control: true }, '\x1b[13;8u'],
];
for (const [modifiers, expected] of mobileEnterModifiers) {
  assert.equal(terminalDataForModifiedEnter(modifiers), expected);
}
assert.equal(terminalDataForModifiedEnter({ meta: true }), '\x1b[13;9u');
assert.equal(terminalDataForModifiedEnter(), undefined);

const navigationKeys = [
  ['left', '\x1b[D', '\x1b[1;8D'],
  ['right', '\x1b[C', '\x1b[1;8C'],
  ['up', '\x1b[A', '\x1b[1;8A'],
  ['down', '\x1b[B', '\x1b[1;8B'],
  ['home', '\x1b[H', '\x1b[1;8H'],
  ['end', '\x1b[F', '\x1b[1;8F'],
  ['page-up', '\x1b[5~', '\x1b[5;8~'],
  ['page-down', '\x1b[6~', '\x1b[6;8~'],
  ['delete', '\x1b[3~', '\x1b[3;8~'],
];
for (const [key, plain, modified] of navigationKeys) {
  assert.equal(terminalDataForNavigationKey(key), plain);
  assert.equal(
    terminalDataForNavigationKey(key, { shift: true, alt: true, control: true }),
    modified,
  );
}
assert.equal(terminalDataForNavigationKey('page-up', { control: true }), '\x1b[5;5~');
assert.equal(terminalDataForNavigationKey('unknown'), undefined);

assert.equal(terminalDataForBeforeInput('deleteWordBackward'), '\x1b\x7f');
assert.equal(terminalDataForBeforeInput('deleteContentBackward'), undefined);
assert.equal(terminalDataForBeforeInput('deleteSoftLineBackward'), undefined);
assert.equal(terminalDataForBeforeInput(''), undefined);
assert.equal(terminalDataForRepeatedMobileBackspace(1), undefined);
assert.equal(terminalDataForRepeatedMobileBackspace(22), undefined);
assert.equal(terminalDataForRepeatedMobileBackspace(23), '\x1b\x7f');
assert.equal(terminalDataForRepeatedMobileBackspace(100), '\x1b\x7f');
assert.equal(normalizeTerminalPasteText('one\r\ntwo\rthree\nfour'), 'one\ntwo\nthree\nfour');
assert.equal(normalizeTerminalPasteText('single line'), 'single line');

console.log('input buffer tests: ok');
