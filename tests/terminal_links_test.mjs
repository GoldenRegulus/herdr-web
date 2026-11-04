import assert from 'node:assert/strict';
import test from 'node:test';
import { openableTerminalUrl, terminalUrls } from '../herdr_web/static/terminal-links.js';

const urls = (text) => terminalUrls(text).map(({ url }) => url);

test('finds http and https URLs in terminal text', () => {
  assert.deepEqual(urls('see https://example.com/a?b=1&c=2#d and http://h:8080/x'), [
    'https://example.com/a?b=1&c=2#d', 'http://h:8080/x',
  ]);
  assert.deepEqual(terminalUrls('go https://e.com now'), [{ start: 3, end: 16, url: 'https://e.com' }]);
  assert.deepEqual(urls('https:// alone, ftp://e.com, nothing'), []);
});

test('leaves sentence punctuation and unopened brackets out of a URL', () => {
  assert.deepEqual(urls('Read https://e.com/x.'), ['https://e.com/x']);
  assert.deepEqual(urls('(see https://e.com/x), then'), ['https://e.com/x']);
  assert.deepEqual(urls('https://en.wikipedia.org/wiki/Shell_(computing)'), ['https://en.wikipedia.org/wiki/Shell_(computing)']);
  assert.deepEqual(urls("'https://e.com/q' \"https://e.com/r\""), ['https://e.com/q', 'https://e.com/r']);
});

test('only web URLs open', () => {
  assert.equal(openableTerminalUrl('https://e.com/a b'), 'https://e.com/a%20b');
  assert.equal(openableTerminalUrl('http://e.com'), 'http://e.com/');
  assert.equal(openableTerminalUrl('javascript:alert(1)'), undefined);
  assert.equal(openableTerminalUrl('file:///etc/passwd'), undefined);
  assert.equal(openableTerminalUrl(undefined), undefined);
});
