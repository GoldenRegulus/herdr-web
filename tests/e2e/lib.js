// Shared helpers for driving herdr-web in Playwright against a real Herdr
// session. See README.md for setup.
const { execFileSync } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');
const { chromium, devices } = require('playwright');

const SESSION = process.env.HERDR_E2E_SESSION || 'herdr-web-e2e';
const BASE = process.env.HERDR_E2E_URL || 'http://127.0.0.1:8799/';
const SHOTS = path.join(os.tmpdir(), 'herdr-web-e2e-shots');
fs.mkdirSync(SHOTS, { recursive: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Herdr commands go to the test session only, never the default one.
function herdr(...args) {
  return execFileSync('herdr', ['--session', SESSION, ...args]).toString();
}
const sendText = (pane, text) => herdr('pane', 'send-text', pane, text);
const sendKeys = (pane, ...keys) => herdr('pane', 'send-keys', pane, ...keys);
const run = (pane, command) => herdr('pane', 'run', pane, command);

const PROFILES = {
  desktop: { viewport: { width: 1280, height: 800 } },
  ios: { ...devices['iPhone 13'], defaultBrowserType: undefined },
  android: { ...devices['Pixel 7'], defaultBrowserType: undefined },
};
const VIEWPORTS = Object.fromEntries(Object.entries(PROFILES).map(([name, p]) => [name, p.viewport]));

async function open(profile) {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    ...PROFILES[profile], permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  if (process.env.HERDR_E2E_TRACE) {
    page.on('websocket', (ws) => ws.on('framesent', (frame) => {
      if (typeof frame.payload !== 'string') console.log('sent', JSON.stringify(frame.payload.toString('utf8')));
    }));
  }
  await page.goto(BASE);
  await page.locator('#backends button', { hasText: SESSION }).first().click();
  await page.waitForSelector('#terminal-view:not([hidden])');
  await page.waitForSelector('#connection-indicator[data-state="connected"]', { state: 'attached', timeout: 15000 });
  await sleep(500);
  return { browser, context, page, errors };
}

async function panesMode(page) {
  await page.evaluate(() => document.querySelector('#panes-mode').click());
  await page.waitForSelector('#panes-view:not([hidden]) .xterm-rows', { timeout: 10000 });
  await sleep(800);
}

// Visible text of every pane terminal, by position in the grid.
function paneTexts(page) {
  return page.$$eval('#pane-grid .xterm-rows', (all) => all.map(
    (rows) => [...rows.children].map((row) => row.textContent.trimEnd()).join('\n').trimEnd(),
  ));
}

// The pane shows `want` as a line of its own: the program ran what was typed.
async function shows(page, want, index = 0, timeout = 3000) {
  const end = Date.now() + timeout;
  let lines = [];
  while (Date.now() < end) {
    lines = ((await paneTexts(page))[index] || '').split('\n');
    if (lines.some((line) => line === want)) return true;
    await sleep(100);
  }
  return `pane ${index} ends ${JSON.stringify(lines.filter(Boolean).slice(-3))}`;
}

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !detail ? '' : ` — ${detail}`}`);
}
const verdict = (name, result) => check(name, result === true, result === true ? '' : result);

// Run named cases, filtered by the optional command-line words.
async function main(runs) {
  const only = process.argv.slice(2);
  for (const [name, body] of Object.entries(runs)) {
    if (only.length && !only.includes(name)) continue;
    // Every profile starts from cleared screens, so no earlier output can
    // satisfy a check.
    for (const pane of ['w1:p1', 'w1:p2']) {
      sendKeys(pane, 'ctrl+c');
      run(pane, 'clear');
    }
    await sleep(500);
    try {
      await body();
    } catch (error) {
      check(`${name} crashed`, false, error.message.split('\n')[0]);
    }
  }
  const failed = results.filter((result) => !result.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}

module.exports = {
  SESSION, SHOTS, VIEWPORTS, sleep, herdr, sendText, sendKeys, run,
  open, panesMode, paneTexts, shows, check, verdict, main,
};
