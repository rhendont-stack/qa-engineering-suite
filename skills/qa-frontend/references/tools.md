# Tools

This skill installs nothing on this machine. Every browser tool runs only through the kernel, in a container from an image pinned by digest. The container has no network except the loopback it shares with the project's own server, started by the same run as its service, and except the run's install step, which fetches the runner's locked packages under a grant recorded with the user's words. The kernel never pulls an image. An engine that does not answer, an image that is not on the machine, or a missing grant makes the run `blocked`: that record is the result. Never run a tool another way (a local install, `brew`, `npm install -g`, `npx`, a browser or binary found on the PATH, a hosted scanner, a page speed website).

Every kernel call is written out in full, from the project root:

`python3 .claude/skills/qa-frontend/scripts/qa.py --role verifier --skill qa-frontend <command>`

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show` | The workspace: the declared environments and the grants. A container run needs no trust: never change `project.trust` |
| `run --container IMAGE@DIGEST [--service IMAGE@DIGEST --service-run SHELL --service-user 65534:65534] [--input .qa/work/frontend/FILE ...] [--install SHELL] --report FORMAT:{evidence}/FILE --shell -- 'SHELL'` | One tool run in a container. The project is copied to `/work` (`.qa/` stays out); an `--input` file is at `/work/.qa-input/frontend/<name>`; `{evidence}` is `/qa-evidence` inside. Only the files a declared `--report` names are kept. Exit codes: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `authorize --environment local --activity install-dependencies --target 127.0.0.1 --approval "<the user's sentence>"` | The grant the browser legs' install step needs (`npm ci` of the committed lockfile inside the container). Only from a sentence of the user's that approves installing the runner's packages in the container, copied whole; never from a sentence that only approves running the project's code |
| `finding add --json .qa/work/frontend/<name>.json`, `finding update FND --json FILE`, `finding state FND duplicate --reason R --duplicate-of KEEP` | One finding, a change to one, one record of a problem marked as a duplicate of another (`references/triage.md`) |
| `state show [executions\|findings\|authorizations]`, `ledger verify` | What was recorded |

The skill's scripts read files and write only under `.qa/work/frontend/`; they run directly, from the project root:

- `python3 .claude/skills/qa-frontend/scripts/static_ui_scan.py --root <site dir> --out .qa/work/frontend/static-ui.sarif` reads the HTML and CSS under the site directory and lists, with `path:line` and WCAG references, every image without a text alternative, click handler on an element that is not a control, form field without a label, email field not typed `email`, and fixed width over 375 CSS px. Every result is `approximate`: read from the source, not from a rendered page. `--copy-to-work` copies the script to `.qa/work/frontend/static_ui_scan.py` for the container leg.
- `python3 .claude/skills/qa-frontend/scripts/run_inputs.py --kind <runner|checks|axe|lighthouse>` writes a leg's inputs into `.qa/work/frontend/` from the committed assets: the runner's `package.json` and `package-lock.json`, `playwright.config.mjs`, the spec, and `pages.json`. `--page PATH` (repeated) names the pages to check as paths on the base URL; `--state-route PATH` names the data request the state checks route, with `--loading`, `--list`, `--empty` and `--error` selectors (`references/states.md`). Call it immediately before every leg that passes those files.
- `python3 .claude/skills/qa-frontend/scripts/wcag_table.py` writes `.qa/work/frontend/wcag-table.json`, one entry per WCAG 2.2 A and AA criterion. `--axe-execution EXE` names the axe run; `--axe-report .qa/evidence/<EXE>/reports/axe.json` reads a stored axe report when axe ran; `--suspected 1.1.1=FND-001` lists a finding against a criterion.

## The images

| Tool | Version | Image (use exactly this) |
|---|---|---|
| Playwright, with its Chromium; also hosts axe-core and Lighthouse from the runner lockfile | 1.65.1 | `mcr.microsoft.com/playwright@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579` |
| Python, for the static scan and for a Python service | 3 (slim) | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` |

The runner lockfile (`assets/runner/package-lock.json`) pins `@playwright/test` 1.65.1 (the image's version, so the image's browsers match), `@axe-core/playwright` 4.14.0, `axe-core` 4.14.0 and `lighthouse` 13.6.0. A service in another runtime uses its image from `.claude/skills/qa-frontend/scripts/_kernel/policies/container-images.json` (for example `node-lts`). An image not in that file is not used.

## The service: the project's own server

`<serve>` is the shell text that starts the project's server in the foreground on `127.0.0.1:8080` from the copy at `/work`, for example `cd /work && exec python3 -B server.py --host 127.0.0.1 --port 8080`. It runs in the service container as an unprivileged user with the copy mounted read-only, and shares only loopback with the browser. Nothing of the project runs on this machine. A static site with no server of its own is served with `cd /work/<site dir> && exec python3 -B -m http.server 8080 --bind 127.0.0.1`. The base URL is always `http://127.0.0.1:8080`: the runner configuration refuses any other host before a browser starts, and the container could not reach one anyway.

## The static UI scan

1. Function: reads every `*.html`, `*.htm` and `*.css` file under a directory with Python's `html.parser` and a CSS rule scan, and writes SARIF 2.1.0 whose results carry `wcag`, `cwe` and `wcag_related` references and `approximate: true`.
2. Use cases: the first step of every check, with or without a browser: images without a text alternative (WCAG 1.1.1), click handlers on elements that are not controls (2.1.1, 4.1.2), fields without labels (1.3.1, 3.3.2, 4.1.2), email fields that accept any text (CWE-20), fixed widths that overflow a 375 px phone (1.4.10).
3. Compatibility: static HTML and CSS, and server templates that keep their markup in `.html` files. Markup built by scripts, inline handlers added from scripts, the cascade and the browser's defaults are not visible to it: a result is a lead for a browser check or a person, never a verdict.
4. Dependencies: Python 3.9 or newer, standard library only; nothing else. In the container leg, the Python image above and the copy written by `--copy-to-work`.
5. Alternatives: axe-core and Lighthouse on the rendered page (below); HTML_CodeSniffer through Pa11y (LGPL-3.0); eslint-plugin-jsx-a11y (MIT) for JSX sources.
6. License, costs, restrictions: part of this skill (Apache-2.0); no network; files over 1 MiB, links and files that are not UTF-8 are listed `unreadable` and not read.
7. Reason selected: it runs everywhere, including where no engine answers, and it cites the file and line a person can open.

```qa-leg frontend-static grant=none report=sarif
python3 .claude/skills/qa-frontend/scripts/qa.py --role verifier --skill qa-frontend run --container python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --category accessibility --timeout 300 --input .qa/work/frontend/static_ui_scan.py --report 'sarif:{evidence}/static-ui.sarif' --shell -- 'cd /work && python3 -I -B /work/.qa-input/frontend/static_ui_scan.py --root . --out /qa-evidence/static-ui.sarif'
```

The kernel reads the SARIF: a run with results is `failed`, and each result opens a provisional finding of the run's category, `accessibility`, with no WCAG reference. So record the host scan's findings with `finding add` first, each with `refs.rule` the scan's rule id, `component.path` and `refs.location` the file, and no `key_attributes` (`references/triage.md`, the finding template): the leg's result for the same rule in the same file is then a new sighting of that finding, not a bare copy. The email field's finding is `functional`, so the leg's result for it opens a copy: make the copy a duplicate of yours (`references/triage.md`, two records of one problem).

## Playwright: the browser checks

1. Function: drives Chromium in the image through `@playwright/test`. `checks.spec.mjs` opens each page of `pages.json` at three viewports (375x812 phone, 768x1024 tablet, 1280x800 desktop) and checks: no console error, page error, failed request or HTTP error response while the page loads; no horizontal overflow; every click target reachable with the Tab key; email fields refusing a value without `@` by the browser's own validation; and, for the data requests `pages.json` names, the loading indicator while a response is held, the error message under a routed HTTP 500, and the empty state under a routed empty list (`references/states.md`). Results go to JUnit (`QA_JUNIT_OUT`), one test case per check, page and viewport, with the viewport in its name.
2. Use cases: "does it work on phones", reflow and overflow, keyboard access to custom controls, console errors on a route, loading, empty and error states, an email field that accepts anything.
3. Compatibility: any site the project's server serves on loopback. Chromium only. A check of an email field reads the browser's constraint validation; a check that runs only on submit is not exercised, because nothing is submitted. A screenshot of each page at each viewport is taken in the overflow check; the kernel keeps declared reports only, so the screenshot itself is not stored: its sha256, pixel size and byte size are recorded in the JUnit report as a `screenshot` property of that test case.
4. Dependencies: the Playwright image; the inputs `package.json`, `package-lock.json`, `playwright.config.mjs`, `checks.spec.mjs` and `pages.json` written by `run_inputs.py --kind checks`; the install step `cd /work/.qa-input/frontend && npm ci --ignore-scripts --no-audit --no-fund` (`--ignore-scripts`: no package script runs; `--no-audit`: an audit would send the dependency list away); an `install-dependencies` grant on `local` for `127.0.0.1`, recorded with the user's words; the service.
5. Alternatives: an existing Cypress, Selenium or WebdriverIO suite of the project, run as it is (below); WebdriverIO (MIT); Puppeteer (Apache-2.0).
6. License, costs, restrictions: Apache-2.0, free, no telemetry. The test step calls `node_modules/.bin/playwright` directly, never `npx`. Chromium runs with `--disable-dev-shm-usage` and with its own sandbox off, inside the confined container. No retries, so a flaky check shows as failed.
7. Reason selected: one runner for checks, axe-core and screenshots, viewport projects, request routing for state checks, and JUnit output the kernel reads.

```qa-leg frontend-checks grant=install report=junit
python3 .claude/skills/qa-frontend/scripts/qa.py --role verifier --skill qa-frontend run --container mcr.microsoft.com/playwright@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579 --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --category functional --timeout 900 --input .qa/work/frontend/package.json --input .qa/work/frontend/package-lock.json --input .qa/work/frontend/playwright.config.mjs --input .qa/work/frontend/checks.spec.mjs --input .qa/work/frontend/pages.json --install 'cd /work/.qa-input/frontend && npm ci --ignore-scripts --no-audit --no-fund' --report 'junit:{evidence}/junit.xml' --shell -- 'sleep 3; QA_BASE_URL=http://127.0.0.1:8080 QA_JUNIT_OUT=/qa-evidence/junit.xml /work/.qa-input/frontend/node_modules/.bin/playwright test --config /work/.qa-input/frontend/playwright.config.mjs'
```

A failed test case names the page, the viewport and what failed; read the failures from `.qa/evidence/<EXE>/reports/junit.xml`. A skipped state check says why (no data request named, or the page requests none of them): it is `not_run`, not passed.

## axe-core: the automated WCAG rules

1. Function: runs axe-core's rules on the rendered page, through `@axe-core/playwright` with the tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa` and `wcag22aa`. `axe.spec.mjs` writes one results object per page of `pages.json` to `QA_AXE_OUT`; the kernel reads it as `axe`: each violation is an alert tagged with its WCAG criteria, an incomplete check a note that needs review.
2. Use cases: the automated part of an accessibility review: text alternatives, contrast, names and roles of controls, labels, language, titles, landmarks and lists.
3. Compatibility: the rendered page at 1280x800 in Chromium. Automated rules decide part of some WCAG criteria and none of the others (`references/wcag-manual.md`); a page with no violation is not a conforming page.
4. Dependencies: as Playwright above, with `axe.spec.mjs` in place of `checks.spec.mjs` (`run_inputs.py --kind axe`), the same install step and grant, and the service.
5. Alternatives: Pa11y (LGPL-3.0, HTML_CodeSniffer or axe); Lighthouse's accessibility category, which runs a subset of axe-core (a secondary signal); IBM Equal Access Checker (Apache-2.0).
6. License, costs, restrictions: MPL-2.0 (axe-core and `@axe-core/playwright`), free, no telemetry. The version is pinned at 4.14.0: rule sets and counts change between versions.
7. Reason selected: the reference rule engine, tagged by WCAG criterion, with no false-positive policy for violations and an explicit "incomplete" list for what a person must review.

```qa-leg frontend-axe grant=install report=axe
python3 .claude/skills/qa-frontend/scripts/qa.py --role verifier --skill qa-frontend run --container mcr.microsoft.com/playwright@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579 --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --category accessibility --timeout 900 --input .qa/work/frontend/package.json --input .qa/work/frontend/package-lock.json --input .qa/work/frontend/playwright.config.mjs --input .qa/work/frontend/axe.spec.mjs --input .qa/work/frontend/pages.json --install 'cd /work/.qa-input/frontend && npm ci --ignore-scripts --no-audit --no-fund' --report 'axe:{evidence}/axe.json' --shell -- 'sleep 3; QA_BASE_URL=http://127.0.0.1:8080 QA_AXE_OUT=/qa-evidence/axe.json /work/.qa-input/frontend/node_modules/.bin/playwright test --config /work/.qa-input/frontend/playwright.config.mjs --project axe-1280x800'
```

A run with a violation is `failed`. Then write the table from it: `wcag_table.py --axe-execution <EXE> --axe-report .qa/evidence/<EXE>/reports/axe.json`.

## Lighthouse: lab metrics and audits

1. Function: loads one URL in Chromium and writes a JSON report (the LHR) with category scores (performance, accessibility, best practices) and lab metrics (first and largest contentful paint, total blocking time, cumulative layout shift, speed index). The kernel records the scores and metrics as measurements beside the run; they never decide its status. The audits themselves (for example `image-alt`, `color-contrast`, `errors-in-console`) are in the stored report.
2. Use cases: a performance and best-practices reading of one page; a second, smaller accessibility signal next to axe-core.
3. Compatibility: one URL per run, mobile emulation by default. A lab run on a shared machine is a measurement, not a verdict, and its numbers vary between runs.
4. Dependencies: the Playwright image (its Chromium, through `CHROME_PATH`); the runner inputs `package.json` and `package-lock.json` (`run_inputs.py --kind lighthouse`); the same install step and grant; the service.
5. Alternatives: Lighthouse CI (Apache-2.0), only with the `filesystem` target: never `lhci upload`, never `temporary-public-storage`; Chrome DevTools traces.
6. License, costs, restrictions: Apache-2.0, free. `--no-enable-error-reporting` keeps its error reporting off; `--output=json` writes the report the kernel reads. Its URL is the loopback service only; a public URL is never given to it, and a public page speed service is never used.
7. Reason selected: the standard lab measurement of web performance, with an accessibility category built on axe-core.

```qa-leg frontend-lighthouse grant=install report=lighthouse
python3 .claude/skills/qa-frontend/scripts/qa.py --role verifier --skill qa-frontend run --container mcr.microsoft.com/playwright@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579 --service python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --service-run '<serve>' --service-user 65534:65534 --category performance --timeout 900 --input .qa/work/frontend/package.json --input .qa/work/frontend/package-lock.json --install 'cd /work/.qa-input/frontend && npm ci --ignore-scripts --no-audit --no-fund' --report 'lighthouse:{evidence}/lighthouse.json' --shell -- 'sleep 3; CHROME_PATH=/ms-playwright/chromium-1255/chrome-linux64/chrome /work/.qa-input/frontend/node_modules/.bin/lighthouse http://127.0.0.1:8080/ --no-enable-error-reporting --output=json --output-path=/qa-evidence/lighthouse.json --only-categories=performance,accessibility,best-practices --quiet --chrome-flags="--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage"'
```

For another page, run the leg again with that page's path after `http://127.0.0.1:8080`.

## Cypress and Selenium: a project's own suite

1. Function: Cypress (MIT) and Selenium WebDriver (Apache-2.0) drive a browser from the project's own end-to-end tests.
2. Use cases: a project that already has such a suite: it runs as it is; this skill never converts it.
3. Compatibility: Cypress needs its binary and a browser; Selenium needs a browser and its driver, or a Selenium server.
4. Dependencies: an image from the catalog that holds them. The catalog pins none, so such a suite is not started here: report it `not_run` with that reason, and hand the run to the skill that runs the project's own suites.
5. Alternatives: the checks above, which need no change to the project.
6. License, costs, restrictions: telemetry off in every run: `CYPRESS_CRASH_REPORTS=0` and `CYPRESS_COMMERCIAL_RECOMMENDATIONS=0` for Cypress, `SE_AVOID_STATS=true` for Selenium, on the run's command line.
7. Reason selected: not selected as this skill's runner; listed so a project's existing suite is run with its telemetry off.

## When a tool cannot run

| The run's record | What it means | What you say |
|---|---|---|
| `blocked`, `dependency_unavailable`, limitation `container_engine_unavailable` | No container engine answered | "<tool> did not run: no container engine on this machine (EXE-…)". No result, no estimate, no score |
| `blocked`, `container_image_missing` | The pinned image is not on the machine; the kernel never pulls | The image the user would need, by digest |
| `blocked`, `not_authorized`, the reason naming `install-dependencies` | No grant for the install step | The approval the user would give, in their own words: installing the runner's locked packages inside the container |
| `blocked`, `policy_denied` | The guard refused the command | Read the reason; never rewrite the command to slip past it |
| `failed` | The tool ran and reported failures or violations | Quote them from the stored report, with the page and viewport |
| `passed` | The tool ran and its checks held | "<tool> ran (EXE-…) and its checks held"; never "accessible" or "conformant" |

A blocked tool's step is not done. The static scan's results and your own reading still stand, under their own names, as `suspected` findings; never relabel them as a browser tool's result.
