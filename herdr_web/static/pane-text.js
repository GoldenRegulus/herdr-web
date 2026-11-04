// Which screen rows go on in the next one, read from Herdr rather than
// guessed. Herdr keeps the terminal state, so the text it copies for a run of
// rows joins the rows the terminal wrapped and puts a line break only where
// the line really ended.

// `rows`: the screen rows as drawn, padded to the pane width. `text`: what
// Herdr copies for those same rows. Returns, for each row but the last,
// whether it goes on in the next row; undefined when the text does not match
// the rows, as when the screen changed in between.
export function rowContinuations(rows, text) {
  const continues = [];
  let at = 0;
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    const content = row.trimEnd();
    if (!text.startsWith(content, at)) return undefined;
    at += content.length;
    if (index === rows.length - 1) break;
    if (at >= text.length || text[at] === '\n') {
      // A real line break, or the end of what Herdr copied: blank rows below
      // the last line have no text.
      continues.push(false);
      if (at < text.length) at += 1;
      continue;
    }
    // A wrapped row goes on with the next one. Herdr keeps the spaces at its
    // end, as when the wrap fell on a space between words.
    const padding = row.length - content.length;
    let spaces = 0;
    while (spaces < padding && text[at + spaces] === ' ') spaces += 1;
    at += spaces;
    continues.push(true);
  }
  return text.slice(at).trim() === '' ? continues : undefined;
}
