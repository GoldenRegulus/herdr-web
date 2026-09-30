import assert from 'node:assert/strict';
import { deflateSync } from 'node:zlib';
import {
  FULL_OUTPUT_COMPRESSION,
  MAX_FULL_OUTPUT_BYTES,
  MAX_PENDING_FULL_OUTPUT_BYTES,
  FullOutputReceiver,
  decompressFullOutput,
  supportsFullOutputCompression,
} from '../herdr_web/static/full-output.js';

const encoder = new TextEncoder();
const buffer = (bytes) => Uint8Array.from(bytes).buffer;
const compressed = (bytes) => buffer(deflateSync(bytes));
const attached = { type: 'attached', output_compression: FULL_OUTPUT_COMPRESSION };
const descriptor = (bytes) => ({ type: 'output-deflate', bytes });
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
function receiver(options = {}) {
  const writes = [];
  const errors = [];
  const instance = new FullOutputReceiver({
    compressionOffered: true,
    isCurrent: () => true,
    write: (bytes) => { writes.push(bytes); },
    onError: (error) => errors.push(error),
    ...options,
  });
  return { instance, writes, errors };
}

assert.equal(supportsFullOutputCompression(), true);
{
  const saved = globalThis.DecompressionStream;
  try {
    globalThis.DecompressionStream = undefined;
    assert.equal(supportsFullOutputCompression(), false);
    globalThis.DecompressionStream = class { constructor() { throw new Error('unsupported'); } };
    assert.equal(supportsFullOutputCompression(), false);
  } finally {
    globalThis.DecompressionStream = saved;
  }
}

{
  // Independent compressed ANSI/UTF-8 chunks, followed by raw, retain wire
  // order even though decompression completes asynchronously. Writes may be
  // enqueued together; accounting is retained until xterm parses each one.
  const parsed = [];
  let acknowledgedBytes = 0;
  const { instance, errors } = receiver({ write: (bytes) => {
    const completion = deferred();
    parsed.push({ bytes, complete: () => { acknowledgedBytes += bytes.length; completion.resolve(); } });
    return completion.promise;
  } });
  instance.control(attached);
  const first = encoder.encode('\x1b[32mλ🙂\x1b[0m\r\n'.repeat(100));
  const second = encoder.encode('\x1b[?2026l');
  instance.control(descriptor(first.length));
  instance.enqueue(compressed(first));
  instance.enqueue(buffer(second));
  await instance.chain;
  assert.equal(parsed.length, 2);
  assert.deepEqual(parsed[0].bytes, first);
  assert.deepEqual(parsed[1].bytes, second);
  assert.equal(acknowledgedBytes, 0);
  assert.equal(instance.pendingBytes, first.length + second.length);
  parsed[0].complete();
  await Promise.resolve();
  assert.equal(acknowledgedBytes, first.length);
  assert.equal(instance.pendingBytes, second.length);
  parsed[1].complete();
  await Promise.resolve();
  assert.equal(acknowledgedBytes, first.length + second.length);
  assert.equal(instance.pendingBytes, 0);
  assert.deepEqual(errors, []);
}

{
  // A new browser attached to an older server stays on the raw protocol.
  const { instance, writes } = receiver();
  instance.control({ type: 'attached' });
  await instance.enqueue(buffer([0, 1, 0xff, 0x1b]));
  assert.deepEqual(writes, [new Uint8Array([0, 1, 0xff, 0x1b])]);
  await instance.enqueue(new ArrayBuffer(256 * 1024));
  assert.equal(writes[1].length, 256 * 1024);
  assert.throws(() => instance.control(descriptor(5)), /not negotiated/);
}
{
  const { instance } = receiver({ compressionOffered: false });
  assert.throws(() => instance.control(attached), /not requested/);
  assert.throws(() => instance.control({ type: 'attached', output_compression: 'gzip' }), /Unsupported/);
}
for (const bytes of [0, -1, 8193, 1.5, true, '8192', NaN]) {
  const { instance } = receiver();
  instance.control(attached);
  assert.throws(() => instance.control(descriptor(bytes)), /size/);
}
for (const next of [descriptor(5), { type: 'ping' }, attached]) {
  const { instance } = receiver();
  instance.control(attached);
  instance.control(descriptor(5));
  assert.throws(() => instance.control(next), /missing its binary/);
  instance.close();
  assert.equal(instance.expectedLength, undefined);
}

for (const [payload, expected] of [
  [new Uint8Array([1, 2, 3]), 40],
  [deflateSync(new Uint8Array(8193)), 8192],
  [deflateSync(new Uint8Array(50)), 60],
  [deflateSync(new Uint8Array(50)).subarray(0, 6), 50],
]) {
  const { instance, writes, errors } = receiver();
  instance.control(attached);
  instance.control(descriptor(expected));
  instance.enqueue(buffer(payload));
  instance.enqueue(buffer([7]));
  await instance.chain;
  assert.equal(writes.length, 0, 'malformed data must not render partial bytes or later output');
  assert.equal(errors.length, 1);
  assert.equal(instance.closed, true);
  assert.equal(instance.pendingBytes, 0);
}

{
  const { instance, writes, errors } = receiver();
  instance.control(attached);
  instance.control(descriptor(8192));
  instance.enqueue(compressed(new Uint8Array(8192)));
  // Let native decompression begin, then close the old flow.
  await Promise.resolve();
  instance.close();
  await instance.chain;
  assert.deepEqual(writes, []);
  assert.deepEqual(errors, []);
  assert.equal(instance.pendingBytes, 0);
  const replacement = receiver();
  replacement.instance.control({ type: 'attached' });
  await replacement.instance.enqueue(buffer([7]));
  assert.deepEqual(replacement.writes, [new Uint8Array([7])]);
}
{
  let current = true;
  const { instance, writes, errors } = receiver({ isCurrent: () => current });
  instance.control(attached);
  instance.control(descriptor(8192));
  instance.enqueue(compressed(new Uint8Array(8192)));
  await Promise.resolve();
  current = false;
  await instance.chain;
  assert.deepEqual(writes, []);
  assert.deepEqual(errors, []);
}
{
  const { instance } = receiver();
  instance.control(attached);
  assert.throws(() => instance.enqueue(new ArrayBuffer(8193)), /size/);
  assert.throws(() => instance.enqueue(new ArrayBuffer(0)), /size/);
  assert.throws(() => instance.enqueue(new Uint8Array(1)), /size/);
}
{
  const parsing = deferred();
  const { instance } = receiver({ write: () => parsing.promise });
  for (let size = 0; size < MAX_PENDING_FULL_OUTPUT_BYTES; size += MAX_FULL_OUTPUT_BYTES) {
    instance.enqueue(new ArrayBuffer(MAX_FULL_OUTPUT_BYTES));
  }
  await instance.chain;
  assert.equal(instance.pendingBytes, MAX_PENDING_FULL_OUTPUT_BYTES);
  assert.throws(() => instance.enqueue(buffer([1])), /queue is full/);
  parsing.resolve();
  await Promise.resolve();
  assert.equal(instance.pendingBytes, 0);
}
{
  const { instance, errors } = receiver({ write: () => Promise.reject(new Error('parser failed')) });
  await instance.enqueue(buffer([1]));
  await Promise.resolve();
  assert.equal(errors.length, 1);
  assert.equal(instance.pendingBytes, 0);
}
{
  const { instance } = receiver();
  instance.control(attached);
  instance.control(descriptor(1));
  assert.throws(() => instance.enqueue(new ArrayBuffer(8192)), /not smaller/);
}
await assert.rejects(decompressFullOutput(new Uint8Array(), 0), /size/);
console.log('Full output compression tests passed');
