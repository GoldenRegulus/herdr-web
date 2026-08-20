const MAX_RETAINED_CAPACITY = 64 * 1024;

export class InputByteBuffer {
  constructor(encoder, initialCapacity = 4096, maximumLength = 16 * 1024 * 1024) {
    this.encoder = encoder;
    this.initialCapacity = initialCapacity;
    this.maximumLength = maximumLength;
    this.bytes = new Uint8Array(initialCapacity);
    this.start = 0;
    this.end = 0;
    this.enqueuedBytes = 0;
    this.consumedBytes = 0;
  }

  get length() {
    return this.end - this.start;
  }

  append(text) {
    if (!text) return;
    const required = Math.max(4, text.length * 3);
    if (this.length + required > this.maximumLength) {
      const encoded = this.encoder.encode(text);
      if (this.length + encoded.length > this.maximumLength) {
        throw new RangeError('terminal input queue is full');
      }
      this.ensureAvailable(encoded.length);
      this.bytes.set(encoded, this.end);
      this.end += encoded.length;
      this.enqueuedBytes += encoded.length;
      return;
    }
    this.ensureAvailable(required);
    const { read, written } = this.encoder.encodeInto(text, this.bytes.subarray(this.end));
    if (read !== text.length) throw new Error('input encoding buffer was too small');
    this.end += written;
    this.enqueuedBytes += written;
  }

  ensureAvailable(required) {
    if (this.bytes.length - this.end >= required) return;
    const length = this.length;
    if (this.bytes.length - length >= required) {
      this.bytes.copyWithin(0, this.start, this.end);
      this.start = 0;
      this.end = length;
      return;
    }
    let capacity = this.bytes.length;
    while (capacity - length < required) capacity *= 2;
    const replacement = new Uint8Array(capacity);
    replacement.set(this.bytes.subarray(this.start, this.end));
    this.bytes = replacement;
    this.start = 0;
    this.end = length;
  }

  peek(maximum) {
    return this.bytes.subarray(this.start, this.start + Math.min(maximum, this.length));
  }

  consume(length) {
    if (length < 0 || length > this.length) throw new RangeError('invalid input buffer consume');
    this.start += length;
    this.consumedBytes += length;
    if (this.start === this.end) {
      this.start = this.end = 0;
      this.releaseExcessCapacity();
    }
  }

  releaseExcessCapacity() {
    if (this.bytes.length > Math.max(this.initialCapacity, MAX_RETAINED_CAPACITY)) {
      this.bytes = new Uint8Array(this.initialCapacity);
    }
  }

  clear() {
    this.start = 0;
    this.end = 0;
    this.enqueuedBytes = 0;
    this.consumedBytes = 0;
    this.releaseExcessCapacity();
  }
}
