import assert from 'node:assert/strict';
import {
  InputByteBuffer,
  isDisposableMouseMotion,
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

console.log('input buffer tests: ok');
