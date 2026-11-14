// The qa-frontend runner configuration (Apache-2.0). Copied to .qa/work/frontend/ by run_inputs.py; do not edit the copy.
//
// It refuses to start unless QA_BASE_URL is an http: URL on 127.0.0.1, [::1] or localhost, with no credentials: the
// check runs here, at load, before any browser starts, and exits 2. The refused value is never printed, so a host
// named by mistake does not reach the run's logs. It imports nothing, so the refusal needs no package either.
//
// Projects: three viewports for checks.spec.mjs (375x812, 768x1024, 1280x800) and one for axe.spec.mjs (1280x800).
// Chromium only, the image's own build, with --disable-dev-shm-usage and its sandbox off inside the confined container.
// No retries (a retry hides a flaky result), one worker, no traces, videos or screenshots on disk. JUnit goes to
// QA_JUNIT_OUT when it is set, with each project in the test name and the annotations as properties.

const LOOPBACK = new Set(['127.0.0.1', '[::1]', 'localhost']);

function loopbackBase(text) {
  let url;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' || !LOOPBACK.has(url.hostname) || url.username !== '' || url.password !== '') {
    return null;
  }
  return url.origin + '/';
}

const base = loopbackBase(process.env.QA_BASE_URL ?? '');
if (base === null) {
  console.error('qa-frontend: refused: QA_BASE_URL must be http:// on 127.0.0.1, [::1] or localhost; no browser was started');
  process.exit(2);
}

const junit = process.env.QA_JUNIT_OUT;
const reporter = [['line']];
if (junit) {
  reporter.push(['junit', { outputFile: junit, includeProjectInTestName: true, embedAnnotationsAsProperties: true }]);
}

const chromium = {
  browserName: 'chromium',
  launchOptions: { args: ['--disable-dev-shm-usage', '--no-sandbox'] },
};

export default {
  testDir: '.',
  outputDir: '/tmp/qa-frontend-results',
  preserveOutput: 'never',
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 60000,
  expect: { timeout: 5000 },
  reporter,
  use: {
    baseURL: base,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
    serviceWorkers: 'block',
    ...chromium,
  },
  projects: [
    { name: 'phone-375x812', testMatch: 'checks.spec.mjs',
      use: { ...chromium, viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } },
    { name: 'tablet-768x1024', testMatch: 'checks.spec.mjs',
      use: { ...chromium, viewport: { width: 768, height: 1024 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } },
    { name: 'desktop-1280x800', testMatch: 'checks.spec.mjs',
      use: { ...chromium, viewport: { width: 1280, height: 800 } } },
    { name: 'axe-1280x800', testMatch: 'axe.spec.mjs',
      use: { ...chromium, viewport: { width: 1280, height: 800 } } },
  ],
};
