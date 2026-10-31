// Web links in terminal text.

// Characters a URL may hold unescaped (RFC 3986), less the quote that
// usually surrounds one in shell output.
const URL_PATTERN = /\bhttps?:\/\/[A-Za-z0-9\-._~:/?#[\]@!$&()*+,;=%]+/g;
const CLOSING = { ')': '(', ']': '[' };

const count = (text, character) => text.split(character).length - 1;

// The http(s) URLs in `text`, as [start, end) offsets. Punctuation that ends
// a sentence is not part of the URL, and neither is a closing bracket the URL
// did not open, as in "(see https://example.com)".
export function terminalUrls(text) {
  const links = [];
  for (const match of String(text).matchAll(URL_PATTERN)) {
    let url = match[0];
    for (;;) {
      const last = url.at(-1);
      if ('.,;:!?*'.includes(last)) {
        url = url.slice(0, -1);
      } else if (CLOSING[last] && count(url, CLOSING[last]) < count(url, last)) {
        url = url.slice(0, -1);
      } else {
        break;
      }
    }
    if (/^https?:\/\/[^/?#]/.test(url)) links.push({ start: match.index, end: match.index + url.length, url });
  }
  return links;
}

// Only web pages open from the terminal; anything else a program prints or
// links stays text.
export function openableTerminalUrl(uri) {
  try {
    const url = new URL(uri);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : undefined;
  } catch (_) {
    return undefined;
  }
}
