// deflate-v1 leaves raw binary messages unchanged. A negotiated descriptor
// applies to exactly the next binary message, never to a later connection.
export const FULL_OUTPUT_COMPRESSION = 'deflate-v1';
export const MAX_FULL_OUTPUT_BYTES = 8 * 1024;
export const MAX_PENDING_FULL_OUTPUT_BYTES = 2 * 1024 * 1024;

export function supportsFullOutputCompression() {
  if (typeof DecompressionStream !== 'function' || typeof Blob !== 'function') return false;
  if (typeof Blob.prototype.stream !== 'function') return false;
  try {
    new DecompressionStream('deflate');
    return true;
  } catch (_) {
    return false;
  }
}

export async function decompressFullOutput(bytes, expectedLength, signal) {
  if (!Number.isInteger(expectedLength) || expectedLength <= 0
      || expectedLength > MAX_FULL_OUTPUT_BYTES || bytes.byteLength > MAX_FULL_OUTPUT_BYTES) {
    throw new Error('Invalid compressed terminal output size');
  }
  const reader = new Blob([bytes]).stream().pipeThrough(
    new DecompressionStream('deflate'),
  ).getReader();
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', cancel, { once: true });
  let completed = false;
  try {
    if (signal?.aborted) throw new Error('Terminal output was canceled');
    const result = new Uint8Array(expectedLength);
    let length = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > expectedLength) throw new Error('Decompressed terminal output is too large');
      result.set(value, length - value.byteLength);
    }
    if (signal?.aborted) throw new Error('Terminal output was canceled');
    if (length !== expectedLength) throw new Error('Decompressed terminal output was truncated');
    completed = true;
    return result;
  } finally {
    signal?.removeEventListener('abort', cancel);
    if (!completed) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export class FullOutputReceiver {
  constructor({ compressionOffered, write, isCurrent, onError }) {
    this.compressionOffered = compressionOffered;
    this.write = write;
    this.isCurrent = isCurrent;
    this.onError = onError;
    this.compression = undefined;
    this.expectedLength = undefined;
    this.pendingBytes = 0;
    this.chain = Promise.resolve();
    this.closed = false;
    this.abort = new AbortController();
  }

  control(message) {
    if (this.closed) return;
    if (this.expectedLength !== undefined) {
      throw new Error('Compressed terminal output is missing its binary payload');
    }
    if (message.type === 'attached') {
      const compression = message.output_compression;
      if (compression != null && compression !== FULL_OUTPUT_COMPRESSION) {
        throw new Error('Unsupported terminal output compression');
      }
      if (compression && !this.compressionOffered) {
        throw new Error('Terminal output compression was not requested');
      }
      this.compression = compression;
    } else if (message.type === 'output-deflate') {
      if (this.compression !== FULL_OUTPUT_COMPRESSION) {
        throw new Error('Terminal output compression was not negotiated');
      }
      if (!Number.isInteger(message.bytes) || message.bytes <= 0
          || message.bytes > MAX_FULL_OUTPUT_BYTES) {
        throw new Error('Invalid compressed terminal output size');
      }
      this.expectedLength = message.bytes;
    }
  }

  enqueue(buffer) {
    if (this.closed) return;
    // Earlier servers may send larger raw PTY bursts. Keep their fallback
    // bounded by the queue limit without imposing the negotiated 8 KiB format.
    const maxWireBytes = this.compression === FULL_OUTPUT_COMPRESSION
      ? MAX_FULL_OUTPUT_BYTES : MAX_PENDING_FULL_OUTPUT_BYTES;
    if (!(buffer instanceof ArrayBuffer) || !buffer.byteLength
        || buffer.byteLength > maxWireBytes) {
      throw new Error('Invalid terminal output size');
    }
    const expectedLength = this.expectedLength;
    this.expectedLength = undefined;
    if (expectedLength !== undefined && buffer.byteLength >= expectedLength) {
      throw new Error('Compressed terminal output is not smaller than its declared size');
    }
    const size = expectedLength ?? buffer.byteLength;
    if (this.pendingBytes + size > MAX_PENDING_FULL_OUTPUT_BYTES) {
      throw new Error('Terminal output queue is full');
    }
    this.pendingBytes += size;
    let released = false;
    const release = () => {
      if (!released) this.pendingBytes -= size;
      released = true;
    };
    this.chain = this.chain.then(async () => {
      try {
        if (this.closed || !this.isCurrent()) return release();
        let bytes = new Uint8Array(buffer);
        if (expectedLength !== undefined) {
          bytes = await decompressFullOutput(bytes, expectedLength, this.abort.signal);
        }
        if (this.closed || !this.isCurrent()) return release();
        // Serialize decoding and enqueueing, but let xterm batch its scheduled
        // writes. Keep bytes charged until its parser callback resolves.
        Promise.resolve(this.write(bytes)).then(release, (error) => {
          release();
          this.fail(error);
        });
      } catch (error) {
        release();
        this.fail(error);
      }
    });
    return this.chain;
  }

  fail(error) {
    if (this.closed) return;
    const current = this.isCurrent();
    this.close();
    if (current) this.onError(error);
  }

  close() {
    this.closed = true;
    this.expectedLength = undefined;
    this.abort.abort();
  }
}
