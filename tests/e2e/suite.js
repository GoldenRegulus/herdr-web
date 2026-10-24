// Core scenarios: desktop Full and Panes, and iOS and Android Panes.
// Usage: node suite.js [desktop] [ios] [android]
const fs = require('fs');
const os = require('os');
const path = require('path');
const L = require('./lib');
const { keyboard } = require('./keyboard');
const { check, verdict, shows, sleep } = L;

async function phone(profile) {
  const { browser, page, errors } = await L.open(profile);
  const k = await keyboard(page, profile);
  const p = (name) => `${profile}: ${name}`;
  try {
    await L.panesMode(page);
    const center = await k.center();
    const below = { x: center.x, y: center.y + 150 };

    await k.swipe(center, below);
    await sleep(100);
    check(p('a fling does not raise the keyboard'), !(await k.focused()));
    await page.touchscreen.tap(center.x, center.y);
    await sleep(100);
    check(p('a tap that stops a fling does not raise the keyboard'), !(await k.focused()));

    await k.blur();
    await sleep(1200);
    await k.swipe(center, below, true);
    await sleep(450);
    check(p('a drag does not raise the keyboard'), !(await k.focused()));
    await k.tap(center);
    check(p('a tap right after a drag raises the keyboard'), await k.focused());
    if (!(await k.focused())) {
      // Measure the input handling below on its own: tap again, later.
      await sleep(1200);
      await k.tap(center);
    }

    await k.type('echo one');
    await k.enter();
    verdict(p('typed characters reach the shell'), await shows(page, 'one'));

    await k.word('echo ');
    await k.word('two');
    await sleep(400);
    await k.enter();
    verdict(p('predictive words reach the shell'), await shows(page, 'two'));

    await k.word('echo ');
    await k.word('quick');
    await k.enter();
    verdict(p('a predictive word followed at once by Enter still runs'), await shows(page, 'quick'));

    await k.type('echo threx');
    await k.backspace();
    await k.type('e');
    await k.enter();
    verdict(p('backspace edits the line'), await shows(page, 'three'));

    await k.type('echo fur');
    const { value } = await k.box();
    await k.select(value.length - 2, value.length - 2);
    await k.type('o');
    await sleep(100);
    await k.enter();
    verdict(p('typing after moving the caret inserts at the caret'), await shows(page, 'four'));

    await k.type('echo ab');
    await k.tapKey('[data-arrow="left"]');
    check(p('an arrow key tap keeps the keyboard up'), await k.focused());
    await k.type('X');
    await sleep(100);
    await k.enter();
    verdict(p('typing after an arrow key lands at the terminal caret'), await shows(page, 'aXb'));

    await k.tapKey('[data-terminal-key="tab"]');
    check(p('a Tab key tap keeps the keyboard up'), await k.focused());
    await k.backspace(3);

    // The shell enables bracketed paste: a two-line paste waits for Enter
    // instead of running its first line.
    const token = `pasted${Date.now()}`;
    await k.paste(`echo ${token}\necho ${token}b`);
    await sleep(800);
    const early = await shows(page, token, 0, 300);
    check(p('a paste to a bracketed-paste shell waits for Enter'), early !== true, 'the first line ran before Enter');
    await k.enter();
    verdict(p('a pasted command runs'), await shows(page, `${token}b`));

    await k.type('echo ');
    await sleep(300);
    await page.evaluate(() => {
      // The iOS paste menu: the field changes and only an input event follows.
      const box = document.activeElement;
      box.value += 'menu';
      box.setSelectionRange(box.value.length, box.value.length);
      box.dispatchEvent(new InputEvent('input', { inputType: 'insertFromPaste', data: null, bubbles: true }));
    });
    await sleep(500);
    await k.type('X');
    await sleep(300);
    await k.enter();
    verdict(p('a paste-menu paste inserts only the pasted text'), await shows(page, 'menuX'));

    // A line longer than the pane wraps; deletes must cross the row boundary.
    const cols = await page.evaluate(() => Math.floor(document.querySelector('#pane-grid .xterm-screen').getBoundingClientRect().width / 8));
    await k.type(`echo ${'w'.repeat(cols + 10)}`);
    await sleep(200);
    await k.backspace(20);
    await sleep(200);
    await k.type('Z');
    await sleep(200);
    await k.enter();
    verdict(p('backspace across a wrapped row'), await shows(page, `${'w'.repeat(cols - 10)}Z`));

    if (profile === 'android') {
      const compose = (text) => k.cdp.send('Input.imeSetComposition', { text, selectionStart: text.length, selectionEnd: text.length });
      await compose('ech');
      await compose('echo');
      await k.word('echo');
      await k.word(' ');
      await compose('composd');
      await compose('composed');
      await k.word('composed');
      await sleep(400);
      await k.enter();
      verdict(p('IME composition commits once'), await shows(page, 'composed'));
    }

    await k.tapKey('#mobile-keyboard-lock');
    await k.tap(center);
    check(p('the keyboard lock keeps the keyboard down'), !(await k.focused()));
    await k.tapKey('#mobile-keyboard-lock');

    check(p('no page errors'), errors.length === 0, errors.join(' | '));
  } finally {
    await page.screenshot({ path: path.join(L.SHOTS, `${profile}-suite.png`) });
    await browser.close();
  }
}

async function desktop() {
  const { browser, page, errors } = await L.open('desktop');
  try {
    // Full mode paints to a canvas: check what the shell wrote instead.
    const out = path.join(os.tmpdir(), `herdr-web-e2e-full-${process.pid}.out`);
    fs.rmSync(out, { force: true });
    await sleep(1000);
    await page.locator('#terminal').click();
    await page.keyboard.type(`echo fulldesk > ${out}\n`, { delay: 10 });
    await sleep(800);
    const written = fs.existsSync(out) ? fs.readFileSync(out, 'utf8') : '(missing)';
    fs.rmSync(out, { force: true });
    check('desktop full: typing reaches the shell', written === 'fulldesk\n', JSON.stringify(written));

    await L.panesMode(page);
    const panes = page.locator('#pane-grid .xterm-screen');
    await panes.nth(0).click();
    await page.keyboard.type('echo left\n', { delay: 10 });
    await panes.nth(1).click();
    await sleep(200);
    await page.keyboard.type('echo right\n', { delay: 10 });
    verdict('desktop panes: typing reaches the left pane', await shows(page, 'left', 0));
    verdict('desktop panes: typing reaches the right pane', await shows(page, 'right', 1));

    // cat -v shows paste markers as ^[[200~ if any reach a program that did
    // not ask for bracketed paste.
    await panes.nth(0).click();
    await page.keyboard.type('cat -v\n', { delay: 10 });
    await sleep(300);
    await page.evaluate(() => {
      const data = new DataTransfer();
      data.setData('text/plain', 'rawpaste');
      document.activeElement.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
    });
    await sleep(500);
    await page.keyboard.press('Enter');
    verdict('desktop panes: the program receives the pasted text', await shows(page, 'rawpaste', 0));
    const shown = (await L.paneTexts(page))[0];
    check('desktop panes: a paste to a program without bracketed paste has no markers', !shown.includes('200~'), JSON.stringify(shown.split('\n').slice(-4)));
    await page.keyboard.press('Control+C');
    await sleep(200);

    await page.setViewportSize({ width: 600, height: 800 });
    await sleep(800);
    await page.locator('#pane-grid .xterm-screen').first().click();
    await page.keyboard.type('echo narrow\n', { delay: 10 });
    verdict('desktop narrow panes: typing reaches the shell', await shows(page, 'narrow', 0));
    check('desktop: no page errors', errors.length === 0, errors.join(' | '));
  } finally {
    await page.screenshot({ path: path.join(L.SHOTS, 'desktop-suite.png') });
    await browser.close();
  }
}

L.main({ desktop, ios: () => phone('ios'), android: () => phone('android') });
