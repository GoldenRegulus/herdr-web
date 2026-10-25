// Edge cases on phones: text from another session, a keyboard dropped and
// raised again, multi-line prompts, hold-to-copy, spacebar caret swipes, and
// small keyboard behaviours. Usage: node edge.js [ios] [android]
// HERDR_E2E_FILTER=word runs only the cases whose name contains it.
const path = require('path');
const L = require('./lib');
const { keyboard } = require('./keyboard');
const { check, verdict, shows, sleep } = L;

const PANE = 'w1:p1';
const EDITOR = `uv run --quiet --no-project --with prompt_toolkit python ${path.join(__dirname, 'ml.py')}`;
const filter = process.env.HERDR_E2E_FILTER;
let sequence = 0;
// Every check looks for its own token, never for text an earlier case printed.
const token = (base) => `${base}${Date.now() % 100000}${sequence++}`;

async function lines(page) {
  return ((await L.paneTexts(page))[0] || '').split('\n');
}

async function phone(profile) {
  const { browser, page, errors } = await L.open(profile);
  const k = await keyboard(page, profile);
  const p = (name) => `${profile}: ${name}`;
  const cases = [];
  const test = (name, body, { fresh = true } = {}) => cases.push({ name, body, fresh });
  // A clean prompt, with the keyboard raised on it.
  const fresh = async () => {
    await k.blur();
    L.sendKeys(PANE, 'ctrl+c');
    L.run(PANE, 'clear');
    await sleep(500);
    await k.tap();
  };

  // ---- text from another session ----
  test('text typed by another session before the keyboard rises is editable', async () => {
    const t = token('o');
    await k.blur();
    L.sendText(PANE, `echo ${t}`);
    await sleep(500);
    await k.tap();
    await k.type('X');
    await k.backspace(2);
    await k.type('Y');
    await k.enter();
    verdict(p('text typed by another session before the keyboard rises is editable'), await shows(page, `${t.slice(0, -1)}Y`));
  });
  test('another session appends while the keyboard is up', async () => {
    const t = token('m');
    await k.type(`echo ${t}`);
    await sleep(400);
    L.sendText(PANE, ' theirs');
    await sleep(600);
    await k.type('!');
    await k.enter();
    verdict(p('another session appends while the keyboard is up'), await shows(page, `${t} theirs!`));
  });
  test('backspace deletes text another session typed', async () => {
    const t = token('b');
    L.sendText(PANE, `echo ${t}abc`);
    await sleep(600);
    await k.backspace(2);
    await k.enter();
    verdict(p('backspace deletes text another session typed'), await shows(page, `${t}a`));
  });
  test('another session clears the line under a draft', async () => {
    const t = token('c');
    await k.type('echo draft');
    await sleep(400);
    L.sendKeys(PANE, 'ctrl+u');
    await sleep(700);
    await k.type(`echo ${t}`);
    await k.enter();
    verdict(p('another session clears the line under a draft'), await shows(page, t));
  });
  test('another session submits the line under a draft', async () => {
    const t = token('s');
    await k.type('echo gone');
    await sleep(400);
    L.sendKeys(PANE, 'Enter');
    await sleep(700);
    await k.type(`echo ${t}`);
    await k.enter();
    verdict(p('another session submits the line under a draft'), await shows(page, t));
  });

  // ---- keyboard dropped and raised again ----
  test('typing continues after the keyboard drops and rises', async () => {
    const t = token('r');
    await k.type(`echo ${t}`);
    await k.blur();
    await sleep(500);
    await k.tap();
    await k.type('d');
    await k.backspace(1);
    await k.type('e');
    await k.enter();
    verdict(p('typing continues after the keyboard drops and rises'), await shows(page, `${t}e`));
  });
  test('the box follows a line changed while the keyboard was down', async () => {
    const t = token('k');
    await k.type(`echo ${t}`);
    await k.blur();
    L.sendText(PANE, 'XYZ');
    await sleep(500);
    await k.tap();
    await k.backspace(1);
    await k.type('!');
    await k.enter();
    verdict(p('the box follows a line changed while the keyboard was down'), await shows(page, `${t}XY!`));
  });
  test('the keyboard rises on a line submitted while it was down', async () => {
    const t = token('n');
    await k.type('echo old');
    await k.blur();
    L.sendKeys(PANE, 'Enter');
    await sleep(500);
    await k.tap();
    await k.type(`echo ${t}`);
    await k.enter();
    verdict(p('the keyboard rises on a line submitted while it was down'), await shows(page, t));
  });
  test('a word still settling when the keyboard drops is not lost', async () => {
    const t = token('w');
    await k.word(`echo ${t}`);
    await k.blur();
    await sleep(500);
    await k.tap();
    await k.enter();
    verdict(p('a word still settling when the keyboard drops is not lost'), await shows(page, t));
  });

  // ---- caret swipes on the spacebar ----
  test('a spacebar caret swipe inserts at the caret', async () => {
    const t = token('h');
    await k.type(`echo ${t} world`);
    await sleep(300);
    const { value } = await k.box();
    await k.sweep(value.length - ' world'.length);
    await k.type('X');
    await k.enter();
    verdict(p('a spacebar caret swipe inserts at the caret'), await shows(page, `${t}X world`));
  });
  test('a caret swipe that overshoots and returns', async () => {
    const t = token('v');
    await k.type(`echo ${t} abc`);
    await sleep(300);
    const { value } = await k.box();
    await k.sweep(value.length - 6, 8);
    await k.sweep(value.length - 2, 8);
    await k.backspace(1);
    await k.enter();
    // Two characters from the end is between a and b.
    verdict(p('a caret swipe that overshoots and returns'), await shows(page, `${t} bc`));
  });
  test('a caret swiped into the prompt shows where typing lands', async () => {
    const t = token('p');
    await k.type(`echo ${t}`);
    await sleep(300);
    const editable = (await k.box()).value.lastIndexOf('echo');
    await k.sweep(2, 5);
    const { start } = await k.box();
    // The prompt cannot be edited: the caret shows at the start of the line.
    check(p('a caret swiped into the prompt shows where typing lands'), start === editable,
      `caret ${start}, editable text starts at ${editable}`);
    await k.type('Z');
    await sleep(300);
    await k.enter();
    verdict(p('typing at that caret goes to the start of the line'), await shows(page, 'bash: Zecho: command not found'));
  });
  test('a selection made with the caret is replaced by typing', async () => {
    const t = token('q');
    await k.type(`echo hello ${t}`);
    await sleep(300);
    const at = (await k.box()).value.indexOf('hello');
    await k.select(at, at + 5);
    await k.type('bye');
    await k.enter();
    verdict(p('a selection made with the caret is replaced by typing'), await shows(page, `bye ${t}`));
  });

  // ---- small keyboard behaviours ----
  test('tab completion is adopted', async () => {
    const t = token('t');
    await k.type('ech');
    await k.tapKey('[data-terminal-key="tab"]');
    await sleep(500);
    await k.type(t);
    await k.enter();
    verdict(p('tab completion is adopted'), await shows(page, t));
  });
  test('a recalled history line is edited', async () => {
    const t = token('y');
    await k.type(`echo ${t}zz`);
    await k.enter();
    await sleep(300);
    await k.tapKey('[data-arrow="up"]');
    await sleep(500);
    await k.backspace(2);
    await k.type('!');
    await k.enter();
    verdict(p('a recalled history line is edited'), await shows(page, `${t}!`));
  });
  test('emoji are deleted whole', async () => {
    const t = token('e');
    await k.type(`echo ${t}`);
    await k.word('👍🏽');
    await sleep(400);
    await k.type('x');
    await k.backspace(2);
    await k.type('k');
    await k.enter();
    verdict(p('emoji are deleted whole'), await shows(page, `${t}k`));
  });
  test('autocorrect replaces a word in the middle', async () => {
    const t = token('a');
    await k.type(`echo teh ${t}`);
    await sleep(300);
    const at = (await k.box()).value.indexOf('teh');
    await k.select(at, at + 3);
    await k.word('the');
    await sleep(400);
    await k.enter();
    verdict(p('autocorrect replaces a word in the middle'), await shows(page, `the ${t}`));
  });
  // The "." shortcut: the space key, pressed after a space, turns it into a
  // period and a space.
  for (const shape of ['two edits', 'one edit']) {
    test(`a double space stays two spaces when iOS sends a period as ${shape}`, async () => {
      const t = token('d');
      await k.type(`echo "${t} `);
      await sleep(200);
      await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
      await page.evaluate((twoEdits) => {
        const box = document.activeElement;
        const edit = (value, inputType, data) => {
          box.value = value;
          box.setSelectionRange(value.length, value.length);
          box.dispatchEvent(new InputEvent('input', { inputType, data, bubbles: true }));
        };
        const before = box.value.slice(0, -1);
        if (twoEdits) edit(before, 'deleteContentBackward', null);
        edit(`${before}. `, 'insertText', '. ');
      }, shape === 'two edits');
      await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
      await sleep(300);
      await k.type('x"');
      await k.enter();
      verdict(p(`a double space stays two spaces when iOS sends a period as ${shape}`), await shows(page, `${t}  x`));
    });
  }
  test('a period and space without the space key are kept', async () => {
    const t = token('n');
    await k.type(`echo ${t} `);
    await sleep(700);
    // Android reports no key: this may be a space after a typed period.
    const { value } = await k.box();
    await k.select(value.length - 1, value.length);
    await k.word('. ');
    await sleep(400);
    await k.type('x');
    await k.enter();
    verdict(p('a period and space without the space key are kept'), await shows(page, `${t}. x`));
  });
  // iOS Smart Punctuation: the hyphen key, pressed after a hyphen, turns that
  // hyphen into a dash. These stand for that keystroke. An iPhone's input log
  // showed it as two edits: the key, a deletion of the hyphen, then the dash.
  const smartDashInTwoEdits = async () => {
    await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: '-', code: 'Minus', windowsVirtualKeyCode: 189 });
    await page.evaluate(() => {
      const box = document.activeElement;
      box.value = box.value.slice(0, -1);
      box.setSelectionRange(box.value.length, box.value.length);
      box.dispatchEvent(new InputEvent('input', { inputType: 'deleteContentBackward', bubbles: true }));
    });
    await k.word('—');
    await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: '-', code: 'Minus', windowsVirtualKeyCode: 189 });
    await sleep(300);
  };
  // The same as one replacement, which other iOS versions may send.
  const smartDashInOneEdit = async () => {
    const { value } = await k.box();
    await k.select(value.length - 1, value.length);
    await k.word('—');
    await sleep(300);
  };
  for (const [shape, smartDash] of [['two edits', smartDashInTwoEdits], ['one edit', smartDashInOneEdit]]) {
    test(`two hyphens stay hyphens when iOS sends the dash as ${shape}`, async () => {
      const t = token('h');
      await k.type(`echo ${t}-`);
      await smartDash();
      await k.type('a');
      await k.type('-');
      await smartDash();
      await smartDash();
      await k.type('b');
      await k.enter();
      verdict(p(`two hyphens stay hyphens when iOS sends the dash as ${shape}`), await shows(page, `${t}--a---b`));
    });
  }
  // WebKit stores a trailing space as a no-break space, then swaps it back.
  const nbspSpace = () => page.evaluate(() => {
    const box = document.activeElement;
    box.value += ' ';
    box.setSelectionRange(box.value.length, box.value.length);
    box.dispatchEvent(new InputEvent('input', { inputType: 'insertText', data: ' ', bubbles: true }));
  });
  test('the box keeps spaces as typed', async () => {
    const whiteSpace = await page.evaluate(() => getComputedStyle(document.activeElement).whiteSpace);
    check(p('the box keeps spaces as typed'), whiteSpace === 'pre', `white-space: ${whiteSpace}`);
  });
  test('a no-break space reaches the shell as a space', async () => {
    const t = token('s');
    await k.type('echo');
    await nbspSpace();
    await sleep(200);
    await k.type(t);
    await k.enter();
    verdict(p('a no-break space reaches the shell as a space'), await shows(page, t));
  });
  test('three periods stay periods when iOS sends an ellipsis', async () => {
    const t = token('e');
    await k.type(`echo ${t}..`);
    // The third period, as an iPhone sent it: the key, two deletions, then …
    await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: '.', code: 'Period', windowsVirtualKeyCode: 190 });
    for (let index = 0; index < 2; index += 1) {
      await page.evaluate(() => {
        const box = document.activeElement;
        box.value = box.value.slice(0, -1);
        box.setSelectionRange(box.value.length, box.value.length);
        box.dispatchEvent(new InputEvent('input', { inputType: 'deleteContentBackward', bubbles: true }));
      });
    }
    await k.word('…');
    await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: '.', code: 'Period', windowsVirtualKeyCode: 190 });
    await sleep(300);
    await k.type('x');
    await k.enter();
    verdict(p('three periods stay periods when iOS sends an ellipsis'), await shows(page, `${t}...x`));
  });
  test('a quote after a no-break space goes out straight', async () => {
    const t = token('q');
    await k.type(`echo ${t}`);
    await nbspSpace();
    await sleep(200);
    // The quote key reports itself; the curly quote replaces the no-break
    // space's place along with a plain space.
    await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: "'", code: 'Quote', windowsVirtualKeyCode: 222 });
    await page.evaluate(() => {
      const box = document.activeElement;
      box.value = `${box.value.slice(0, -1)} ‘`;
      box.setSelectionRange(box.value.length, box.value.length);
      box.dispatchEvent(new InputEvent('input', { inputType: 'insertText', data: '‘', bubbles: true }));
    });
    await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: "'", code: 'Quote', windowsVirtualKeyCode: 222 });
    await sleep(200);
    await k.type("x'");
    await k.enter();
    verdict(p('a quote after a no-break space goes out straight'), await shows(page, `${t} x`));
  });
  test('a dash picked on purpose is kept', async () => {
    const t = token('j');
    await k.type(`echo ${t}`);
    await k.word('—');
    await k.word('–');
    await sleep(300);
    await k.enter();
    verdict(p('a dash picked on purpose is kept'), await shows(page, `${t}—–`));
  });
  test('quotes typed on the quote key stay straight', async () => {
    const t = token('i');
    await k.type(`echo ${t}`);
    // The quote key reports itself and inserts the curly character.
    for (const [key, text] of [["'", '‘'], ["'", '’'], ['"', '“'], ['"', '”']]) {
      await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key, text, unmodifiedText: key });
      await k.cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key });
      await sleep(60);
    }
    await k.type('x');
    await k.enter();
    // bash removes the quotes it received: '' and "" are empty strings.
    verdict(p('quotes typed on the quote key stay straight'), await shows(page, `${t}x`));
  });
  test('a curly quote picked on purpose is kept', async () => {
    const t = token('k');
    await k.type(`echo ${t}`);
    await sleep(700);
    await k.word('’');
    await sleep(300);
    await k.enter();
    verdict(p('a curly quote picked on purpose is kept'), await shows(page, `${t}’`));
  });
  test('select all and delete clears only the line', async () => {
    const t = token('l');
    await k.type('echo junk');
    await sleep(300);
    await page.evaluate(() => {
      document.activeElement.select();
      document.dispatchEvent(new Event('selectionchange'));
    });
    await k.backspace(1);
    await sleep(300);
    await k.type(`echo ${t}`);
    await k.enter();
    verdict(p('select all and delete clears only the line'), await shows(page, t));
  });
  test('a word delete removes the word', async () => {
    const t = token('x');
    await k.type(`echo ${t} two`);
    await sleep(300);
    await page.evaluate(() => {
      const box = document.activeElement;
      box.value = box.value.slice(0, -3);
      box.setSelectionRange(box.value.length, box.value.length);
      box.dispatchEvent(new InputEvent('input', { inputType: 'deleteWordBackward', bubbles: true }));
    });
    await sleep(300);
    await k.type('3');
    await k.enter();
    verdict(p('a word delete removes the word'), await shows(page, `${t} 3`));
  });
  test('Ctrl C clears the line', async () => {
    const t = token('z');
    await k.type('echo never');
    await k.tapKey('[data-modifier="control"]');
    await k.type('c');
    await sleep(400);
    await k.type(`echo ${t}`);
    await k.enter();
    const shown = await lines(page);
    check(p('Ctrl C clears the line'), shown.includes(t) && !shown.includes('never'), JSON.stringify(shown.filter(Boolean).slice(-3)));
  });
  test('rotating while typing keeps the line', async () => {
    const t = token('g');
    await k.type(`echo ${t}${'r'.repeat(50)}`);
    await page.setViewportSize({ width: 844, height: 390 });
    await sleep(1000);
    await k.tap();
    await k.backspace(3);
    await k.type('Q');
    await k.enter();
    const result = await shows(page, `${t}${'r'.repeat(47)}Q`);
    await page.setViewportSize(L.VIEWPORTS[profile]);
    await sleep(1000);
    verdict(p('rotating while typing keeps the line'), result);
  });
  test('switching tabs and back keeps the line', async () => {
    const t = token('u');
    await k.type(`echo ${t}`);
    await sleep(300);
    for (const tab of ['2', '1']) {
      await page.evaluate(() => document.querySelector('[data-sheet="tabs"]').click());
      await sleep(500);
      await page.locator('#sheet-content button', { hasText: tab }).first().click();
      await sleep(1200);
    }
    await k.tap();
    await k.type('Z');
    await k.enter();
    verdict(p('switching tabs and back keeps the line'), await shows(page, `${t}Z`));
  });
  test('a fast burst of typing arrives intact', async () => {
    const t = token('f');
    const text = `echo ${t}-the-quick-brown-fox`;
    if (profile === 'ios') await page.keyboard.type(text);
    else for (const character of text) await k.cdp.send('Input.insertText', { text: character });
    await k.enter();
    verdict(p('a fast burst of typing arrives intact'), await shows(page, `${t}-the-quick-brown-fox`));
  });

  // ---- multi-line input, as in an agent prompt (see ml.py) ----
  const submit = async () => {
    await k.tapKey('[data-terminal-key="escape"]');
    await k.enter();
    await sleep(500);
  };
  test('lines typed with Enter in a multi-line prompt', async () => {
    await k.blur();
    L.run(PANE, EDITOR);
    const end = Date.now() + 30000;
    while (Date.now() < end && (await lines(page)).filter(Boolean).at(-1) !== '>') await sleep(250);
    await k.tap();
    await k.type('first');
    await k.enter();
    await k.type('second');
    await submit();
    verdict(p('lines typed with Enter in a multi-line prompt'), await shows(page, "RESULT='first\\nsecond'"));
  });
  test('backspace joins lines in a multi-line prompt', async () => {
    await k.type('aa');
    await k.enter();
    await k.type('bb');
    await k.backspace(3);
    await k.type('X');
    await submit();
    verdict(p('backspace joins lines in a multi-line prompt'), await shows(page, "RESULT='aaX'"));
  }, { fresh: false });
  test('typing after moving up a line in a multi-line prompt', async () => {
    await k.type('one');
    await k.enter();
    await k.type('two');
    await k.tapKey('[data-arrow="up"]');
    await sleep(300);
    await k.type('Z');
    await submit();
    verdict(p('typing after moving up a line in a multi-line prompt'), await shows(page, "RESULT='oneZ\\ntwo'"));
  }, { fresh: false });
  test('a multi-line paste into a multi-line prompt', async () => {
    await k.paste('p1\np2');
    await sleep(600);
    await k.type('!');
    await submit();
    verdict(p('a multi-line paste into a multi-line prompt'), await shows(page, "RESULT='p1\\np2!'"));
  }, { fresh: false });
  test('a long wrapped line in a multi-line prompt', async () => {
    const cols = await page.evaluate(() => Math.floor(document.querySelector('#pane-grid .xterm-screen').getBoundingClientRect().width / 8));
    await k.type('w'.repeat(cols + 5));
    await sleep(300);
    await k.backspace(10);
    await k.type('E');
    await submit();
    const joined = (await lines(page)).join('');
    check(p('a long wrapped line in a multi-line prompt'), joined.includes(`RESULT='${'w'.repeat(cols - 5)}E'`), joined.slice(-80));
    // Ctrl+C at an empty prompt_toolkit prompt ends ml.py, back to bash.
    L.sendKeys(PANE, 'ctrl+c');
    await sleep(800);
  }, { fresh: false });

  // ---- hold-to-copy ----
  // Long-press to open the snapshot, select from `offset` in the row starting
  // with `from` to `length` characters into the row starting with `to`, and
  // copy.
  const copyCase = (name, command, [from, offset, to, length], expected) => test(name, async () => {
    await k.blur();
    L.run(PANE, command);
    await sleep(800);
    const box = await page.locator('#pane-grid .xterm-screen').first().boundingBox();
    await k.longPress({ x: box.x + 30, y: box.y + 30 });
    await page.waitForSelector('.pane-snapshot-view[data-ready="true"]', { timeout: 5000 });
    await sleep(300);
    const copied = await page.evaluate(([first, start, last, end]) => {
      const rows = [...document.querySelectorAll('.pane-snapshot-view .xterm-rows > div')];
      const a = rows.findIndex((row) => row.textContent.startsWith(first));
      const b = rows.findIndex((row, index) => index >= a && row.textContent.startsWith(last));
      const text = (row) => {
        const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
        walker.nextNode();
        return walker.currentNode;
      };
      document.getSelection().setBaseAndExtent(text(rows[a]), start, text(rows[b]), end);
      const data = new DataTransfer();
      rows[a].dispatchEvent(new ClipboardEvent('copy', { clipboardData: data, bubbles: true, cancelable: true }));
      // Nothing set means the browser's own copy of the selection is used.
      return data.getData('text/plain') || String(document.getSelection());
    }, [from, offset, to, length]);
    const want = typeof expected === 'function' ? await expected() : expected;
    check(p(name), copied === want, `got ${JSON.stringify(copied)} want ${JSON.stringify(want)}`);
    await page.evaluate(() => document.getSelection().removeAllRanges());
    await sleep(600);
    await page.locator('.pane-snapshot-view').click({ force: true }).catch(() => {});
    await sleep(500);
  });
  const words = Array.from({ length: 16 }, (_, index) => `word${index}`).join(' ');
  copyCase('copy across a wrapped line and real newlines',
    `printf '%s\\nhard one\\nhard two\\n' '${words}'`, ['word0', 0, 'hard two', 8],
    `${words}\nhard one\nhard two`);
  copyCase('copy that starts and ends inside rows',
    `printf '%s\\nhard one\\nhard two\\n' '${words}'`, ['word0', 6, 'hard two', 4],
    `${words.slice(6)}\nhard one\nhard`);
  // Sized so that the wrap falls on the space between two words.
  copyCase('copy keeps a space where a wrap falls on a word break',
    `printf '%s\\n' "$(printf 'x%.0s' $(seq $(($(tput cols) - 1)))) next"`, ['xxxx', 0, 'next', 4],
    async () => `${'x'.repeat(await page.evaluate(() => [...document.querySelectorAll('.pane-snapshot-view .xterm-rows > div')]
      .find((row) => row.textContent.startsWith('xxxx')).textContent.trimEnd().length))} next`);
  copyCase('copy keeps spaces inside a line and blank lines',
    "printf 'a  b   c\\n\\nlast\\n'", ['a  b', 0, 'last', 4],
    'a  b   c\n\nlast');

  try {
    for (const { name, body, fresh: clean } of cases) {
      if (filter && !name.includes(filter)) continue;
      if (clean) await fresh();
      try {
        await body();
      } catch (error) {
        check(p(`${name} crashed`), false, error.message.split('\n')[0]);
      }
    }
    check(p('no page errors'), errors.length === 0, errors.join(' | '));
  } finally {
    await page.screenshot({ path: path.join(L.SHOTS, `${profile}-edge.png`) });
    await browser.close();
  }
}

L.main({ ios: () => phone('ios'), android: () => phone('android') });
