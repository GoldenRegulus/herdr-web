import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { InputTransfer, INPUT_TRANSFER_CHUNK_BYTES } from '../herdr_web/static/input-transfer.js';
import { InputByteBuffer } from '../herdr_web/static/input-buffer.js';

const app = readFileSync(new URL('../herdr_web/static/app.js', import.meta.url), 'utf8');
function slice(start, end) {
  const from = app.indexOf(start);
  const to = app.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  return app.slice(from, to);
}

function socket({ fast = false } = {}) {
  return {
    readyState: 1,
    bufferedAmount: 0,
    sent: [],
    send(data) {
      this.sent.push(typeof data === 'string' ? data : Uint8Array.from(data instanceof ArrayBuffer ? new Uint8Array(data) : data));
      if (!fast) this.bufferedAmount += typeof data === 'string'
        ? new TextEncoder().encode(data).length : data.byteLength;
    },
  };
}

function harness({ fast = false, negotiated = true } = {}) {
  const ws = socket({ fast });
  const flow = { socket: ws, attached: true, inputReady: true,
    inputChunkBytes: negotiated ? INPUT_TRANSFER_CHUNK_BYTES : 0 };
  const inputBuffer = new InputByteBuffer(new TextEncoder());
  const operations = [];
  const timers = [];
  const messages = [];
  const context = vm.createContext({
    socket: ws, outputFlow: flow, inputBuffer, inputOperations: operations,
    InputTransfer, WebSocket: { OPEN: 1 }, INPUT_BATCH_BYTES: 16 * 1024,
    INPUT_WEBSOCKET_HIGH_WATER_BYTES: 32 * 1024,
    inputDrainTimer: undefined, pendingPaneActivation: undefined,
    setTimeout(callback) { timers.push(callback); return timers.length; },
    clearTimeout() {},
    showBrowserToast(message) { messages.push(message); },
    setActivePane() { throw Error('unexpected pane activation'); },
    mobileQuery: { matches: false }, paneTerminals: new Map(),
  });
  vm.runInContext(slice('  function clearOutputFlow(', '  function sendOutputAcknowledgement('), context);
  vm.runInContext(slice('  function inputBytesBeforeOperation()', '  function abandonHttpSession('), context);
  vm.runInContext('function drainInput() { drainWebSocketInput(socket, outputFlow); }', context);
  return { ws, flow, context, operations, inputBuffer, timers, messages,
    drain() { vm.runInContext('drainInput()', context); },
    tick() { ws.bufferedAmount = 0; timers.shift()?.(); },
    append(operation) { operations.push({ ready: true, offset: inputBuffer.enqueuedBytes, ...operation }); },
  };
}

const binaryFrames = (ws) => ws.sent.filter((item) => typeof item !== 'string');
const controls = (ws) => ws.sent.filter((item) => typeof item === 'string').map(JSON.parse);

test('16 MiB image uses bounded chunks and leaves room for parser ACKs', () => {
  const h = harness();
  const image = new Uint8Array(16 * 1024 * 1024).fill(42);
  h.append({ kind: 'clipboard-image', bytes: image.buffer, extension: 'png' });
  h.drain();
  assert.equal(binaryFrames(h.ws).length, 1);
  assert.equal(h.operations.length, 1);
  assert.ok(h.ws.bufferedAmount <= 32 * 1024);
  h.ws.send(JSON.stringify({ type: 'output-ack', bytes: 8192 }));
  for (let i = 0; h.operations.length && i < 1024; i += 1) h.tick();
  assert.equal(h.operations.length, 0);
  assert.equal(binaryFrames(h.ws).length, 1024);
  assert.ok(binaryFrames(h.ws).every((frame) => frame.length <= 16 * 1024));
  assert.equal(binaryFrames(h.ws).reduce((total, frame) => total + frame.length, 0), image.length);
  assert.equal(controls(h.ws)[1].type, 'output-ack');
});

test('fast sockets yield one bulk chunk per task and preserve key/paste order', () => {
  const h = harness({ fast: true });
  h.inputBuffer.append('before');
  const text = '🙂'.repeat(10000);
  h.append({ kind: 'pane-paste', streamId: 3, text });
  h.inputBuffer.append('after');
  h.drain();
  assert.equal(binaryFrames(h.ws).length, 2); // preceding key plus one chunk
  assert.equal(new TextDecoder().decode(binaryFrames(h.ws)[0]), 'before');
  assert.equal(h.operations.length, 1);
  h.ws.send(JSON.stringify({ type: 'pane-output-ack', stream_id: 3, seq: '1' }));
  for (let i = 0; h.operations.length && i < 10; i += 1) h.tick();
  h.tick();
  const frames = binaryFrames(h.ws);
  assert.equal(new TextDecoder().decode(frames.at(-1)), 'after');
  assert.equal(Buffer.concat(frames.slice(1, -1)).toString(), text);
  assert.equal(controls(h.ws)[0].stream_id, 3);
});

test('strict high-water budget delays a chunk instead of overshooting', () => {
  const ws = socket();
  const transfer = new InputTransfer({ kind: 'clipboard-image', extension: 'png',
    bytes: new Uint8Array(50_000).buffer }, 16 * 1024);
  ws.bufferedAmount = 30_000;
  assert.equal(transfer.sendNext(ws, 32 * 1024), false);
  assert.equal(binaryFrames(ws).length, 0);
  assert.ok(ws.bufferedAmount <= 32 * 1024);
  ws.bufferedAmount = 0;
  assert.equal(transfer.sendNext(ws, 32 * 1024), false);
  assert.equal(binaryFrames(ws).length, 1);
});

test('clearing input cancels a partial transfer on its owning socket', () => {
  const h = harness();
  h.append({ kind: 'clipboard-image', bytes: new Uint8Array(50_000).buffer, extension: 'png' });
  h.drain();
  vm.runInContext('clearInputOperations()', h.context);
  assert.equal(controls(h.ws).at(-1).type, 'input-transfer-cancel');
  assert.equal(h.flow.inputTransfer, undefined);
  assert.equal(h.operations.length, 0);
  h.inputBuffer.append('new key');
  h.tick();
  assert.equal(new TextDecoder().decode(binaryFrames(h.ws).at(-1)), 'new key');
});

test('reconnect never replays partial bulk body and keeps unsent keys once', () => {
  const h = harness();
  h.append({ kind: 'clipboard-image', bytes: new Uint8Array(50_000).buffer, extension: 'png' });
  h.inputBuffer.append('after');
  h.drain();
  vm.runInContext('clearOutputFlow()', h.context);
  const replacement = socket();
  h.context.socket = replacement;
  h.context.outputFlow = { socket: replacement, attached: true, inputReady: true, inputChunkBytes: 16 * 1024 };
  h.tick();
  assert.deepEqual(controls(replacement), []);
  assert.equal(binaryFrames(replacement).length, 1);
  assert.equal(new TextDecoder().decode(binaryFrames(replacement)[0]), 'after');
  assert.equal(h.operations.length, 0);
  assert.equal(h.messages.length, 1);
});

test('older servers receive the unchanged legacy message shapes', () => {
  const h = harness({ negotiated: false });
  h.append({ kind: 'pane-paste', streamId: 3, text: 'text' });
  h.append({ kind: 'clipboard-image', bytes: new Uint8Array([1, 2]).buffer, extension: 'png' });
  h.drain();
  assert.deepEqual(controls(h.ws).map((control) => control.type), ['pane-paste', 'clipboard-image']);
  assert.deepEqual([...binaryFrames(h.ws)[0]], [1, 2]);
});
