# Report flags and re-run selection per framework

Read the row for the framework in use. Flags are the ones the kernel's `qa detect tests` reports for the framework; confirm a flag with the tool's own `--help` before relying on it, because versions differ. `{evidence}` in a path is replaced by the execution's evidence directory. A report path that does not use `{evidence}` is relative to the directory `qa run` is started in.

A framework-level retry (a retry count in the configuration of the tool) can hide a failure that happened before a pass. Do not turn one on. If the project already has one on, the report may list the test as passed after a retry: report that test as flaky.

## Report flags

| Framework | Command shape | Report to declare with `--report` | Notes |
|---|---|---|---|
| pytest | `python3 -m pytest --junitxml={evidence}/junit.xml` | `junit:{evidence}/junit.xml` | Do not add plugins that rerun failures (for example `--reruns`): they hide flakiness. |
| Jest | `npx jest --reporters=default --reporters=jest-junit` | `junit:junit.xml` | Needs the `jest-junit` package already installed; it writes `junit.xml` in the working directory unless `JEST_JUNIT_OUTPUT_DIR` is set. If the package is absent, report `blocked`; do not install it. |
| Vitest | `npx vitest run --reporter=junit --outputFile.junit={evidence}/junit.xml` | `junit:{evidence}/junit.xml` | Use `run`, never watch mode. |
| Playwright | `npx playwright test --reporter=junit` | `junit:<path>` | The JUnit reporter writes to the file named by the environment variable `PLAYWRIGHT_JUNIT_OUTPUT_FILE`, or to stdout otherwise. A test that passed after a configured retry is listed as flaky in Playwright's own report: surface it. |
| Cypress | `npx cypress run --reporter junit --reporter-options mochaFile={evidence}/junit.xml` | `junit:{evidence}/junit.xml` | Cypress needs its binary already cached; a missing one is `blocked`. |
| Gradle | `./gradlew test` | `junit:build/test-results/test/*.xml` (per module for a multi-module build) | JUnit XML is written by default. Gradle skips a test task it considers up to date, which would give a stale report: run `./gradlew cleanTest test`. The wrapper may try to download a Gradle distribution; with no network that is `blocked`, not something to work around. |
| Maven | `mvn test` | `junit:target/surefire-reports/TEST-*.xml` | Surefire writes these by default. Add `-o` (offline) when the network is not allowed. Do not add `-DfailIfNoTests=false` or `-Dsurefire.failIfNoSpecifiedTests=false`: they turn an empty selection into a pass. |
| go test | `go test -json ./...` | none built in | No JUnit output without a converter (`references/tools.md`). Without a report the result has only the exit code as evidence and is recorded with that weaker strength; say so. Add `-count=1` so the build cache does not replay an old result. |
| cargo test | `cargo test` | none built in | No JUnit output without `cargo-nextest` (`references/tools.md`). Same limitation as go test. |

## Selecting one test to re-run in isolation (flaky protocol)

The re-run must see the same commit, the same environment and the same isolation as the run it repeats. Select the failed test by the narrowest name the framework supports, keep the report flags, and add `--attempt-of <EXE>` to `qa run`.

| Framework | Select one test | Notes |
|---|---|---|
| pytest | `python3 -m pytest "tests/test_sync.py::test_sync_settles"` (node ID, with `::Class::method` for a class) | `--lf` reruns the last failures from pytest's cache and falls back to everything when nothing failed: prefer the node ID. |
| Jest | `npx jest path/to/file.test.js -t "exact test name"` | `-t` is a regular expression: anchor it, escape special characters. |
| Vitest | `npx vitest run path/to/file.test.ts -t "exact test name"` | |
| Playwright | `npx playwright test path/to/file.spec.ts:LINE` or `-g "title"`; `--last-failed` after a failed run | `--last-failed` needs a recent Playwright; if the flag is unknown, use the file and title. |
| Cypress | `npx cypress run --spec path/to/spec.cy.js` | Selection is by spec file; a single test inside a spec needs `.only`, which edits the test: do not. Re-run the whole spec instead. |
| Gradle | `./gradlew cleanTest test --tests "pkg.ClassName.methodName"` | `cleanTest` forces the run. |
| Maven | `mvn test -Dtest='ClassName#methodName'` | Quote the argument. |
| go test | `go test -run '^TestName$' -count=1 ./pkg/...` | Anchored so a prefix does not select several tests. |
| cargo test | `cargo test test_name -- --exact` | |

## Tests that are never retried automatically

Destructive tests (they delete or mutate data), tests that use a shared environment, and load tests. Report them `failed`, state that they were not retried, and leave the decision to the user.
