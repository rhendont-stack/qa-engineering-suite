// The qa-frontend axe-core run (Apache-2.0; axe-core and @axe-core/playwright are MPL-2.0). Copied to .qa/work/frontend/
// by run_inputs.py; do not edit the copy.
//
// Opens each page of pages.json on the base URL the config accepted (a path that leaves it is refused) and runs axe-core
// through @axe-core/playwright with the WCAG 2.0, 2.1 and 2.2 level A and AA rule tags. Writes the array of results
// objects, one per page, to QA_AXE_OUT, a new file directly in /qa-evidence/ (an existing file is never overwritten), for
// the run's `--report axe:{evidence}/axe.json`. The test itself asserts nothing about the results: the kernel reads them,
// and a violation makes the run `failed`.

import { readFileSync, writeFileSync } from 'node:fs';
import { test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];
const settings = JSON.parse(readFileSync(new URL('./pages.json', import.meta.url), 'utf8'));
const pages = Array.isArray(settings.pages) && settings.pages.length ? settings.pages : ['/'];

function output() {
  const out = process.env.QA_AXE_OUT ?? '';
  if (!/^\/qa-evidence\/[A-Za-z0-9._-]+\.json$/.test(out)) {
    throw new Error('QA_AXE_OUT must name a .json file directly in /qa-evidence/');
  }
  return out;
}

test('axe-core, WCAG A and AA rules, on every page in pages.json', async ({ page, baseURL }) => {
  const out = output();
  const base = new URL(baseURL);
  const results = [];
  for (const path of pages) {
    const url = new URL(path, base);
    if (url.origin !== base.origin) {
      throw new Error('refused: a page outside the base URL');
    }
    await page.goto(url.href, { waitUntil: 'load' });
    await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});
    results.push(await new AxeBuilder({ page }).withTags(TAGS).analyze());
  }
  writeFileSync(out, JSON.stringify(results, null, 1) + '\n', { flag: 'wx' });
  console.log(`axe-core ${results[0].testEngine.version}: ${results.length} page(s), `
    + results.map((r) => `${new URL(r.url).pathname} ${r.violations.length} violated rule(s), ${r.incomplete.length} to review`).join('; '));
});
