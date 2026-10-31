// What a phone does to the page: taps, swipes, and soft keyboard input.
// iOS keyboards send real key events; Android ones send only input events
// (keyCode 229), which CDP Input.insertText reproduces.
const { sleep } = require('./lib');

async function keyboard(page, profile) {
  const cdp = await page.context().newCDPSession(page);
  const k = {
    cdp,
    focused() {
      return page.evaluate(() => document.activeElement?.classList.contains('xterm-helper-textarea') === true);
    },
    async center() {
      const box = await page.locator('#pane-grid').boundingBox();
      return { x: box.x + box.width / 2, y: box.y + box.height / 3 };
    },
    async tap(point) {
      const at = point || await k.center();
      await page.touchscreen.tap(at.x, at.y);
      await sleep(300);
    },
    async tapKey(selector) {
      const box = await page.locator(selector).first().boundingBox();
      await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
      await sleep(200);
    },
    // A fling leaves momentum behind; a slow drag (about 0.15 px/ms) does not
    // outlast ~350 ms.
    async swipe(from, to, slow = false) {
      const point = (p) => [{ x: p.x, y: p.y, id: 1 }];
      const steps = slow ? 30 : 6;
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(from) });
      for (let step = 1; step <= steps; step += 1) {
        const p = { x: from.x + ((to.x - from.x) * step) / steps, y: from.y + ((to.y - from.y) * step) / steps };
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: point(p) });
        await sleep(slow ? 33 : 16);
      }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    },
    async longPress(point, ms = 800) {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ ...point, id: 1 }] });
      await sleep(ms);
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    },
    async blur() {
      await page.evaluate(() => document.activeElement?.blur());
      await sleep(300);
    },
    async type(text) {
      if (profile === 'ios') return page.keyboard.type(text, { delay: 30 });
      for (const character of text) {
        await cdp.send('Input.insertText', { text: character });
        await sleep(30);
      }
    },
    // A predictive word, swipe word or dictation result: one input event.
    word(text) { return cdp.send('Input.insertText', { text }); },
    async enter() { await page.keyboard.press('Enter'); await sleep(150); },
    async backspace(count = 1) {
      for (let index = 0; index < count; index += 1) {
        await page.keyboard.press('Backspace');
        await sleep(40);
      }
    },
    box() {
      return page.evaluate(() => {
        const box = document.activeElement;
        return { value: box.value, start: box.selectionStart, end: box.selectionEnd };
      });
    },
    // The spacebar trackpad moves the native caret one step at a time.
    async sweep(to, stepMs = 15) {
      let { start } = await k.box();
      while (start !== to) {
        start += start < to ? 1 : -1;
        await page.evaluate((at) => {
          document.activeElement.setSelectionRange(at, at);
          document.dispatchEvent(new Event('selectionchange'));
        }, start);
        await sleep(stepMs);
      }
      await sleep(100);
    },
    async select(from, to) {
      await page.evaluate(([start, end]) => {
        document.activeElement.setSelectionRange(start, end);
        document.dispatchEvent(new Event('selectionchange'));
      }, [from, to]);
      await sleep(80);
    },
    paste(text) {
      return page.evaluate((value) => {
        const data = new DataTransfer();
        data.setData('text/plain', value);
        document.activeElement.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
      }, text);
    },
  };
  return k;
}

module.exports = { keyboard };
