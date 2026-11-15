// The axe corpus's runner: load one page in the image's Chromium and write axe-core's results object as JSON.
// Usage: node axe-run.mjs URL OUTPUT. The URL must be on loopback, as every URL of a run is.
import { writeFileSync } from 'node:fs';
import { chromium } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const [url, output] = process.argv.slice(2);
if (!/^http:\/\/127\.0\.0\.1:[0-9]+\//.test(url ?? '') || !output) {
  console.error('usage: node axe-run.mjs http://127.0.0.1:PORT/ OUTPUT');
  process.exit(2);
}
const browser = await chromium.launch({ args: ['--no-sandbox'] });
try {
  const context = await browser.newContext(); // axe-core's Playwright wrapper needs a page that belongs to a context
  const page = await context.newPage();
  await page.goto(url);
  const results = await new AxeBuilder({ page }).analyze();
  writeFileSync(output, JSON.stringify(results, null, 2) + '\n');
  console.log(`axe-core ${results.testEngine.version}: ${results.violations.length} violations, ${results.incomplete.length} incomplete, ` +
    `${results.passes.length} passes, ${results.inapplicable.length} inapplicable`);
} finally {
  await browser.close();
}
