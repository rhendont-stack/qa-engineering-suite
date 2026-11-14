// The qa-frontend browser checks (Apache-2.0). Copied to .qa/work/frontend/ by run_inputs.py; do not edit the copy.
//
// For each page in pages.json, in each viewport project of playwright.config.mjs:
//   - no console error, page error, failed request or HTTP error response while the page loads;
//   - no horizontal overflow (the document is no wider than the layout viewport), with one screenshot of the viewport
//     whose sha256, size and dimensions are recorded as an annotation (the kernel keeps declared reports only);
//   - every visible click target (an onclick attribute, a button-like role or a pointer cursor, outside a native
//     control) receives keyboard focus within a run of Tab presses;
//   - an email field (type email, autocomplete email, or a name or id naming email) refuses a value without "@" by the
//     browser's own constraint validation (a check that runs only on submit is not exercised: nothing is submitted);
//   - for the data requests pages.json names (states.routes): the loading indicator shows while a response is held and
//     clears after it; under a routed HTTP 500 the indicator clears and an error message shows; under a routed empty
//     list the list or an empty-state message says so.
// Every page is opened on the base URL the config accepted; a path that leaves it is refused. Nothing is submitted.

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';

const settings = JSON.parse(readFileSync(new URL('./pages.json', import.meta.url), 'utf8'));
const pages = Array.isArray(settings.pages) && settings.pages.length ? settings.pages : ['/'];
const states = settings.states ?? {};
const routes = Array.isArray(states.routes) ? states.routes : [];
const SETTLE_MS = 500;

function onBase(baseURL, path) {
  const base = new URL(baseURL);
  const url = new URL(path, base);
  if (url.origin !== base.origin) {
    throw new Error('refused: a page outside the base URL');
  }
  return url.href;
}

async function open(page, baseURL, path) {
  await page.goto(onBase(baseURL, path), { waitUntil: 'load' });
  await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(SETTLE_MS);
}

function routed(url) {
  return routes.includes(url.pathname);
}

function short(text) {
  return String(text).replace(/\s+/g, ' ').slice(0, 200);
}

function pngSize(buffer) {
  return buffer.length >= 24 ? `${buffer.readUInt32BE(16)}x${buffer.readUInt32BE(20)}` : 'unknown';
}

for (const path of pages) {
  test(`${path}: no console error, page error or failed request`, async ({ page, baseURL }) => {
    const problems = [];
    page.on('console', (message) => {
      if (message.type() === 'error') problems.push(`console error: ${short(message.text())}`);
    });
    page.on('pageerror', (error) => problems.push(`page error: ${short(error.message)}`));
    page.on('requestfailed', (request) => problems.push(`request failed: ${new URL(request.url()).pathname} ${short(request.failure()?.errorText ?? '')}`));
    page.on('response', (response) => {
      if (response.status() >= 400) problems.push(`HTTP ${response.status()}: ${new URL(response.url()).pathname}`);
    });
    await open(page, baseURL, path);
    expect(problems, `${path} logged ${problems.length} problem(s) while loading`).toEqual([]);
  });

  test(`${path}: no horizontal overflow`, async ({ page, baseURL }) => {
    await open(page, baseURL, path);
    const shot = await page.screenshot({ fullPage: false });
    const viewport = page.viewportSize();
    test.info().annotations.push({
      type: 'screenshot',
      description: `sha256:${createHash('sha256').update(shot).digest('hex')} ${pngSize(shot)} px, ${shot.length} bytes, viewport ${viewport.width}x${viewport.height}`,
    });
    const found = await page.evaluate(() => {
      const width = document.documentElement.clientWidth;
      const name = (el) => el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '') + [...el.classList].map((c) => `.${c}`).join('');
      const wide = [...document.querySelectorAll('body *')].filter((el) => {
        const right = el.getBoundingClientRect().right;
        const parent = el.parentElement ? el.parentElement.getBoundingClientRect().right : 0;
        return right > width + 1 && parent <= width + 1;
      });
      return { scroll: document.documentElement.scrollWidth, width, wide: wide.slice(0, 5).map((el) => `${name(el)} reaches ${Math.round(el.getBoundingClientRect().right)} px`) };
    });
    expect(found.scroll, `${path} is ${found.scroll} px wide in a ${found.width} px viewport: ${found.wide.join('; ') || 'no single element named'}`)
      .toBeLessThanOrEqual(found.width);
  });

  test(`${path}: every click target is reachable by keyboard`, async ({ page, baseURL }) => {
    await open(page, baseURL, path);
    const presses = await page.evaluate(() => {
      const native = 'a[href], area[href], button, input, select, textarea, summary, label, option, details, [contenteditable]';
      const visible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
      const clickable = (el) => el.hasAttribute('onclick') || ['button', 'link', 'checkbox', 'tab', 'menuitem', 'switch'].includes(el.getAttribute('role'))
        || getComputedStyle(el).cursor === 'pointer';
      const candidates = [...document.querySelectorAll('body *')].filter((el) => visible(el) && clickable(el) && !el.matches(native)
        && !el.closest('a[href], button, label, summary'));
      const targets = candidates.filter((el) => !candidates.some((other) => other !== el && other.contains(el)));
      window.__qaFrontendTargets = targets;
      window.__qaFrontendReached = new Set();
      if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
      const focusable = document.querySelectorAll('a[href], area[href], button, input, select, textarea, summary, iframe, [tabindex], [contenteditable]').length;
      return Math.min(200, Math.max(20, focusable + 10));
    });
    for (let i = 0; i < presses; i += 1) {
      await page.keyboard.press('Tab');
      await page.evaluate(() => {
        const active = document.activeElement;
        if (!active || active === document.body) return;
        window.__qaFrontendTargets.forEach((target, index) => {
          if (target === active || target.contains(active)) window.__qaFrontendReached.add(index);
        });
      });
    }
    const missed = await page.evaluate(() => window.__qaFrontendTargets
      .filter((_, index) => !window.__qaFrontendReached.has(index))
      .map((el) => el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '') + [...el.classList].map((c) => `.${c}`).join('')
        + (el.hasAttribute('onclick') ? '[onclick]' : '') + ` "${(el.textContent || '').trim().slice(0, 40)}"`));
    expect(missed, `${path}: ${missed.length} click target(s) never received keyboard focus in ${presses} Tab presses`).toEqual([]);
  });

  test(`${path}: email fields refuse a value without @`, async ({ page, baseURL }) => {
    await open(page, baseURL, path);
    const accepted = await page.evaluate(() => {
      const named = /e[-_]?mail/i;
      const fields = [...document.querySelectorAll('input')].filter((el) => el.type !== 'hidden' && !el.disabled && !el.readOnly
        && el.getClientRects().length > 0 && (el.type === 'email' || (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/).includes('email')
        || named.test(el.name || '') || named.test(el.id || '')));
      const found = [];
      for (const el of fields) {
        const before = el.value;
        el.value = 'not-an-address';
        if (el.checkValidity()) found.push(`input${el.id ? `#${el.id}` : ''}[type=${el.type}] accepts "not-an-address"`);
        el.value = before;
      }
      return { fields: fields.length, found };
    });
    test.skip(accepted.fields === 0, 'no email field on this page');
    expect(accepted.found, `${path}: ${accepted.found.length} email field(s) accept any text`).toEqual([]);
  });

  test(`${path}: loading state while the data request is pending`, async ({ page, baseURL }) => {
    test.skip(routes.length === 0, 'pages.json names no data request (states.routes)');
    test.skip(!states.loading, 'pages.json names no loading indicator (states.loading)');
    let hits = 0;
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    await page.route(routed, async (route) => {
      hits += 1;
      await held;
      await route.continue();
    });
    try {
      await page.goto(onBase(baseURL, path), { waitUntil: 'load' });
      await expect.poll(() => hits, { timeout: 3000 }).toBeGreaterThan(0).catch(() => {});
      test.skip(hits === 0, 'the page requests none of the state routes');
      await expect.soft(page.locator(states.loading).first(), 'no loading indicator while the data request is pending').toBeVisible({ timeout: 3000 });
    } finally {
      release();
    }
    await expect.soft(page.locator(states.loading).first(), 'the loading indicator stays after the data arrived').toBeHidden({ timeout: 5000 });
  });

  test(`${path}: error state under a routed HTTP 500`, async ({ page, baseURL }) => {
    test.skip(routes.length === 0, 'pages.json names no data request (states.routes)');
    let hits = 0;
    await page.route(routed, async (route) => {
      hits += 1;
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"answered 500 by the qa-frontend check"}' });
    });
    await open(page, baseURL, path);
    test.skip(hits === 0, 'the page requests none of the state routes');
    if (states.loading) {
      await expect.soft(page.locator(states.loading).first(), 'the loading indicator stays after the HTTP 500').toBeHidden({ timeout: 3000 });
    }
    await expect.soft(page.locator(states.error ?? '[role=alert]').first(), 'no error message shows after the HTTP 500')
      .toBeVisible({ timeout: 3000 });
  });

  test(`${path}: empty state under a routed empty list`, async ({ page, baseURL }) => {
    test.skip(routes.length === 0, 'pages.json names no data request (states.routes)');
    test.skip(!states.list && !states.empty, 'pages.json names neither the list nor an empty-state message (states.list, states.empty)');
    let hits = 0;
    await page.route(routed, async (route) => {
      hits += 1;
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await open(page, baseURL, path);
    test.skip(hits === 0, 'the page requests none of the state routes');
    if (states.loading) {
      await expect.soft(page.locator(states.loading).first(), 'the loading indicator stays after the empty list').toBeHidden({ timeout: 3000 });
    }
    const message = states.empty ? await page.locator(states.empty).first().isVisible() : false;
    const listed = states.list ? (await page.locator(states.list).first().innerText().catch(() => '')).trim() : '';
    expect.soft(message || listed.length > 0, 'the list shows nothing and no message says it is empty').toBe(true);
  });
}
