const DELETE_CHARACTER = '\x7f';
export const MOBILE_PREDICTION_TEXT_LIMIT = 1024;
const graphemeSegmenter = typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function'
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  : undefined;

function graphemes(text) {
  if (graphemeSegmenter) {
    return [...graphemeSegmenter.segment(text)].map((part) => part.segment);
  }
  return [...text];
}

function validText(text) {
  return typeof text === 'string'
    && text.length <= MOBILE_PREDICTION_TEXT_LIMIT
    && !/[\x00-\x1f\x7f]/u.test(text);
}

function caretIndex(parts, offset) {
  if (!Number.isInteger(offset) || offset < 0) return undefined;
  let length = 0;
  for (let index = 0; index <= parts.length; index += 1) {
    if (length === offset) return index;
    if (index < parts.length) length += parts[index].length;
  }
  return undefined;
}

function moveCaret(from, to, applicationCursorKeys) {
  const prefix = applicationCursorKeys ? '\x1bO' : '\x1b[';
  return (prefix + (to < from ? 'D' : 'C')).repeat(Math.abs(to - from));
}

export function terminalCaretInput(text, from, to, applicationCursorKeys = false) {
  if (!validText(text)) return undefined;
  const parts = graphemes(text);
  const start = caretIndex(parts, from);
  const end = caretIndex(parts, to);
  if (start === undefined || end === undefined) return undefined;
  return moveCaret(start, end, applicationCursorKeys);
}

export function terminalTextBeforeCursor(terminal) {
  const buffer = terminal?.buffer?.active;
  if (!buffer) return '';
  let row = buffer.baseY + buffer.cursorY;
  let line = buffer.getLine(row);
  if (!line) return '';

  const parts = [line.translateToString(false, 0, buffer.cursorX)];
  while (line.isWrapped && row > 0 && parts.join('').length < MOBILE_PREDICTION_TEXT_LIMIT) {
    row -= 1;
    line = buffer.getLine(row);
    if (!line) break;
    parts.unshift(line.translateToString(false));
  }
  return parts.join('');
}

// Read only enough physical rows to confirm the owned text. Herdr's explicit
// cursor positioning does not always retain xterm's isWrapped markers.
function textAroundCursor(terminal, limit) {
  const buffer = terminal?.buffer?.active;
  if (!buffer) return undefined;
  const row = buffer.baseY + buffer.cursorY;
  const line = buffer.getLine(row);
  if (!line) return undefined;
  let before = line.translateToString(false, 0, buffer.cursorX);
  let after = line.translateToString(false, buffer.cursorX);
  for (let step = 1; step <= limit && before.length < limit && row >= step; step += 1) {
    const previous = buffer.getLine(row - step);
    if (!previous) break;
    before = previous.translateToString(false) + before;
  }
  for (let step = 1; step <= limit && after.length < limit; step += 1) {
    const next = buffer.getLine(row + step);
    if (!next) break;
    after += next.translateToString(false);
  }
  return { before: before.slice(-limit), after: after.slice(0, limit) };
}

export function terminalHasEditableSuffix(terminal, text) {
  if (!validText(text) || !text) return false;
  return textAroundCursor(terminal, text.length)?.before.endsWith(text) === true;
}

export function terminalHasEditableText(terminal, text) {
  if (!validText(text) || !text) return false;
  const around = textAroundCursor(terminal, text.length);
  if (!around) return false;
  const joined = around.before + around.after;
  // A rendered frame can still show an earlier caret position while ordered
  // movement is in flight. Confirm the complete owned text across that cursor,
  // rather than discarding the model on each delayed cursor echo.
  const start = joined.indexOf(text, Math.max(0, around.before.length - text.length));
  return start >= 0 && start <= around.before.length
    && start + text.length >= around.before.length;
}

export function terminalTextInputDelta(
  previousText, nextText,
  previousCursor = previousText?.length, nextCursor = nextText?.length,
  applicationCursorKeys = false,
) {
  if (!validText(previousText) || !validText(nextText)) return undefined;
  const previous = graphemes(previousText);
  const next = graphemes(nextText);
  const from = caretIndex(previous, previousCursor);
  const to = caretIndex(next, nextCursor);
  if (from === undefined || to === undefined) return undefined;
  let shared = 0;
  while (shared < previous.length && shared < next.length
    && previous[shared] === next[shared]) shared += 1;
  if (shared === previous.length && shared === next.length) {
    return { data: moveCaret(from, to, applicationCursorKeys), removed: 0, inserted: '' };
  }
  let tail = 0;
  while (tail < previous.length - shared && tail < next.length - shared
    && previous[previous.length - tail - 1] === next[next.length - tail - 1]) tail += 1;
  const removed = previous.length - shared - tail;
  const inserted = next.slice(shared, next.length - tail).join('');
  return {
    data: moveCaret(from, shared + removed, applicationCursorKeys)
      + DELETE_CHARACTER.repeat(removed) + inserted
      + moveCaret(next.length - tail, to, applicationCursorKeys),
    removed,
    inserted,
  };
}

export function terminalPredictionReplacement({
  text, selectionStart, selectionEnd, replacement, editable,
  cursor = text?.length, applicationCursorKeys = false,
}) {
  if (!editable || !validText(text) || !validText(replacement)
    || !Number.isInteger(selectionStart) || !Number.isInteger(selectionEnd)
    || selectionStart < 0 || selectionStart >= selectionEnd
    || selectionEnd > text.length
    || terminalCaretInput(text, selectionStart, selectionEnd) === undefined) return undefined;
  const nextText = text.slice(0, selectionStart) + replacement + text.slice(selectionEnd);
  if (nextText === text) return undefined;
  const nextCursor = selectionStart + replacement.length;
  const edit = terminalTextInputDelta(text, nextText, cursor, nextCursor, applicationCursorKeys);
  if (!edit) return undefined;
  return { data: edit.data, text: nextText, cursor: nextCursor };
}
