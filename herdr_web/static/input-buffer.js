const MAX_RETAINED_CAPACITY = 64 * 1024;
const SGR_MOUSE_EVENT = /^\x1b\[<(\d+);\d+;\d+M$/;
const DELETE_CHARACTER = '\x7f';
const META_BACKSPACE = '\x1b\x7f';
const IOS_CHARACTER_BACKSPACES_BEFORE_WORDS = 22;
const CSI_FINAL_NAVIGATION_KEYS = {
  left: 'D',
  right: 'C',
  up: 'A',
  down: 'B',
  home: 'H',
  end: 'F',
};
const CSI_TILDE_NAVIGATION_KEYS = {
  'page-up': 5,
  'page-down': 6,
  delete: 3,
};

export function terminalDataForModifiedEnter(modifiers = {}) {
  const modifierBits = Number(modifiers.shift === true)
    + Number(modifiers.alt === true) * 2
    + Number(modifiers.control === true) * 4
    + Number(modifiers.meta === true) * 8;
  return modifierBits ? `\x1b[13;${modifierBits + 1}u` : undefined;
}

export function terminalDataForNavigationKey(key, modifiers = {}) {
  const final = CSI_FINAL_NAVIGATION_KEYS[key];
  const tilde = CSI_TILDE_NAVIGATION_KEYS[key];
  if (final === undefined && tilde === undefined) return undefined;
  const modifierBits = Number(modifiers.shift === true)
    + Number(modifiers.alt === true) * 2
    + Number(modifiers.control === true) * 4;
  if (!modifierBits) return final === undefined ? `\x1b[${tilde}~` : `\x1b[${final}`;
  const modifier = modifierBits + 1;
  return final === undefined
    ? `\x1b[${tilde};${modifier}~`
    : `\x1b[1;${modifier}${final}`;
}

export function terminalDataForBeforeInput(inputType) {
  if (inputType === 'deleteContentBackward') return DELETE_CHARACTER;
  if (inputType === 'deleteWordBackward') return META_BACKSPACE;
  return undefined;
}

export function terminalDataForRepeatedMobileBackspace(count) {
  return count > IOS_CHARACTER_BACKSPACES_BEFORE_WORDS ? META_BACKSPACE : undefined;
}

export function normalizeTerminalPasteText(text) {
  return text.replace(/\r\n?/g, '\n');
}

export function isDisposableMouseMotion(data) {
  const sgr = SGR_MOUSE_EVENT.exec(data);
  if (sgr) return (Number(sgr[1]) & 32) !== 0;

  // X10 and UTF-8 mouse protocols encode the button value at this position.
  if (data.startsWith('\x1b[M') && data.length === 6) {
    const button = data.charCodeAt(3) - 32;
    return button >= 0 && (button & 32) !== 0;
  }
  return false;
}

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
