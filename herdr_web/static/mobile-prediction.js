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
    // A line break is text: multi-line input must stay editable.
    && !/[\x00-\x09\x0b-\x1f\x7f]/u.test(text);
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

// The fold around the cursor covers the text being composed or confirmed.
// Rows join with no break when they are one wrapped line. A hard row
// boundary joins with a line break only when the composed text has that
// break. A row the text does not cover is program output and stays out.
function tailMatch(candidate, target) {
  const limit = Math.min(candidate.length, target.length);
  for (let size = limit; size > 0; size -= 1) {
    if (candidate.endsWith(target.slice(target.length - size))) return size;
  }
  return 0;
}

function headMatch(candidate, target) {
  const limit = Math.min(candidate.length, target.length);
  for (let size = limit; size > 0; size -= 1) {
    if (target.startsWith(candidate.slice(0, size))) return size;
  }
  return 0;
}

export function terminalTextBeforeCursor(terminal, composed) {
  const shadow = terminalTextAtCursor(terminal, composed);
  return shadow.text.slice(0, shadow.cursor);
}

export function terminalTextAtCursor(terminal, composed) {
  const buffer = terminal?.buffer?.active;
  if (!buffer) return { text: '', cursor: 0 };
  const cursorRow = buffer.baseY + buffer.cursorY;
  const line = buffer.getLine(cursorRow);
  if (!line) return { text: '', cursor: 0 };

  const composedText = typeof composed?.text === 'string' ? composed.text : '';
  const composedCursor = Number.isInteger(composed?.cursor)
    ? Math.max(0, Math.min(composedText.length, composed.cursor))
    : composedText.length;
  const targetBefore = composedText.slice(0, composedCursor);
  const targetAfter = composedText.slice(composedCursor);

  let row = cursorRow;
  let before = line.translateToString(false, 0, buffer.cursorX);
  while (row > 0 && before.length < MOBILE_PREDICTION_TEXT_LIMIT) {
    if (targetBefore && before.endsWith(targetBefore)) break;
    const wrapped = buffer.getLine(row)?.isWrapped === true;
    const above = buffer.getLine(row - 1);
    if (!above) break;
    const chunk = above.translateToString(true);
    let best;
    for (const sep of wrapped ? [''] : ['', '\n']) {
      const candidate = chunk + sep + before;
      const match = targetBefore ? tailMatch(candidate, targetBefore) : (wrapped ? 1 : 0);
      if (!best || match > best.match) best = { candidate, match };
    }
    if (!best || best.match <= 0) break;
    before = best.candidate;
    row -= 1;
  }
  let after = line.translateToString(true, buffer.cursorX);
  row = cursorRow + 1;
  let next = buffer.getLine(row);
  while (next && before.length + after.length < MOBILE_PREDICTION_TEXT_LIMIT) {
    if (targetAfter && after.startsWith(targetAfter)) break;
    const wrapped = next.isWrapped === true;
    const chunk = next.translateToString(true);
    let best;
    for (const sep of wrapped ? [''] : ['', '\n']) {
      const candidate = after + sep + chunk;
      const match = targetAfter ? headMatch(candidate, targetAfter) : (wrapped ? 1 : 0);
      if (!best || match > best.match) best = { candidate, match };
    }
    if (!best || best.match <= 0) break;
    after = best.candidate;
    row += 1;
    next = buffer.getLine(row);
  }
  after = after.trimEnd();
  const start = Math.max(0, before.length + after.length - MOBILE_PREDICTION_TEXT_LIMIT);
  return {
    text: (before + after).slice(start, start + MOBILE_PREDICTION_TEXT_LIMIT),
    cursor: Math.max(0, before.length - start),
  };
}

function lineBreaks(text) {
  return (typeof text === 'string' ? text.match(/\n/g) : null)?.length || 0;
}

// Read enough physical rows to find or confirm the owned text. This window
// searches, so rows fold freely; only the join follows the text, since its
// line breaks are the only hard boundaries it can know.
function textAroundCursor(terminal, text, limit = text.length) {
  const buffer = terminal?.buffer?.active;
  if (!buffer) return undefined;
  const row = buffer.baseY + buffer.cursorY;
  const line = buffer.getLine(row);
  if (!line) return undefined;
  let breaks = lineBreaks(text);
  let before = line.translateToString(false, 0, buffer.cursorX);
  let after = line.translateToString(false, buffer.cursorX);
  for (let step = 1; step <= limit && before.length < limit && row >= step; step += 1) {
    const wrapped = buffer.getLine(row - step + 1)?.isWrapped === true;
    let sep = '';
    if (!wrapped && breaks > 0) {
      sep = '\n';
      breaks -= 1;
    }
    const previous = buffer.getLine(row - step);
    if (!previous) break;
    before = previous.translateToString(false) + sep + before;
  }
  for (let step = 1; step <= limit && after.length < limit; step += 1) {
    const next = buffer.getLine(row + step);
    if (!next) break;
    const wrapped = next.isWrapped === true;
    let sep = '';
    if (!wrapped && breaks > 0) {
      sep = '\n';
      breaks -= 1;
    }
    after += sep + next.translateToString(false);
  }
  return { before: before.slice(-limit), after: after.slice(0, limit) };
}

export function terminalHasEditableSuffix(terminal, text) {
  if (!validText(text) || !text) return false;
  return textAroundCursor(terminal, text)?.before.endsWith(text) === true;
}

export function terminalHasEditableText(terminal, text) {
  if (!validText(text) || !text) return false;
  const around = textAroundCursor(terminal, text);
  if (!around) return false;
  const joined = around.before + around.after;
  // A rendered frame can still show an earlier caret position while ordered
  // movement is in flight. Confirm the complete owned text across that cursor,
  // rather than discarding the model on each delayed cursor echo.
  const start = joined.indexOf(text, Math.max(0, around.before.length - text.length));
  return start >= 0 && start <= around.before.length
    && start + text.length >= around.before.length;
}

export function terminalPredictionPrefix(
  terminal, text = '', cursor = text.length, existingPrefix,
) {
  if (!validText(text) || terminalCaretInput(text, cursor, cursor) === undefined) return undefined;
  let before = terminalTextBeforeCursor(terminal, { text, cursor });
  const maximumPrefixLength = MOBILE_PREDICTION_TEXT_LIMIT - text.length;
  if (!text) return before.slice(-maximumPrefixLength);
  if (!terminalHasEditableText(terminal, text)) return undefined;
  // If Herdr used explicit cursor positioning for a wrapped row, xterm might
  // not have an isWrapped marker. Keep the known prefix when the current
  // physical row contains only an ending fragment of the editable text.
  if (!before || text.endsWith(before)) {
    if (typeof existingPrefix === 'string') {
      if (maximumPrefixLength === 0) return '';
      return existingPrefix.slice(-maximumPrefixLength);
    }
    before = textAroundCursor(terminal, text, MOBILE_PREDICTION_TEXT_LIMIT)?.before || before;
  }
  let editableBeforeCursor = '';
  let candidate = '';
  for (const part of graphemes(text)) {
    candidate += part;
    if (before.endsWith(candidate)) editableBeforeCursor = candidate;
  }
  if (maximumPrefixLength === 0) return '';
  return before.slice(0, before.length - editableBeforeCursor.length).slice(-maximumPrefixLength);
}

export function mobileTextWithoutRedundantSeparator(
  previousText, nextText, nextCursor, staticPrefix = '',
) {
  if (!validText(previousText) || !validText(nextText)
    || terminalCaretInput(nextText, nextCursor, nextCursor) === undefined) return undefined;
  const previous = graphemes(previousText);
  const next = graphemes(nextText);
  let shared = 0;
  while (shared < previous.length && shared < next.length
    && previous[shared] === next[shared]) shared += 1;
  let tail = 0;
  while (tail < previous.length - shared && tail < next.length - shared
    && previous[previous.length - tail - 1] === next[next.length - tail - 1]) tail += 1;
  const inserted = next.slice(shared, next.length - tail);
  const before = shared > 0 ? previous[shared - 1] : graphemes(staticPrefix).at(-1);
  if (inserted[0] !== ' ' || !/^\s$/u.test(before || '')) {
    return { text: nextText, cursor: nextCursor };
  }
  const separatorOffset = next.slice(0, shared).join('').length;
  return {
    text: next.slice(0, shared).join('') + next.slice(shared + 1).join(''),
    cursor: nextCursor > separatorOffset ? nextCursor - 1 : nextCursor,
  };
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
    return {
      data: moveCaret(from, to, applicationCursorKeys),
      removed: 0,
      inserted: '',
      insertedStart: 0,
      insertedEnd: 0,
    };
  }
  let tail = 0;
  while (tail < previous.length - shared && tail < next.length - shared
    && previous[previous.length - tail - 1] === next[next.length - tail - 1]) tail += 1;
  const removed = previous.length - shared - tail;
  const inserted = next.slice(shared, next.length - tail).join('');
  const insertedStart = next.slice(0, shared).join('').length;
  return {
    data: moveCaret(from, shared + removed, applicationCursorKeys)
      + DELETE_CHARACTER.repeat(removed) + inserted
      + moveCaret(next.length - tail, to, applicationCursorKeys),
    removed,
    inserted,
    insertedStart,
    insertedEnd: insertedStart + inserted.length,
  };
}
