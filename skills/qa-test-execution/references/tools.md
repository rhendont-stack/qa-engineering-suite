# Tools

Nothing here is installed by this skill. A tool that is missing makes the execution `blocked`; say what is missing and what the user could do.

## Kernel commands used

All through `python3 <skill-dir>/scripts/qa.py --role verifier --skill qa-test-execution <command>`.

| Command | Use |
|---|---|
| `init` | Create `.qa/` in the project (safe to repeat). |
| `config show` | Read `project.trust` and `defaults.flaky_reruns`. |
| `config set project.trust trusted --approval "<the user's words>"` | Record the user's approval to run the tests on this machine. Only with their words. |
| `detect tests` | Candidate commands ranked CI, documented, inferred, each with its source and report flag. Reads files; runs nothing. |
| `doctor` | Kernel integrity, ledger problems, configuration drift, tool versions on this machine, Docker availability. |
| `run [--report FORMAT:PATH] [--timeout S] [--scope targeted\|full] [--attempt-of EXE] -- <command>` | Run one command under the guard and record it. Exit code: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run. |
| `run --container IMAGE@DIGEST -- <command>` | The same, inside a container with no network. The image must already be on the machine; the kernel never pulls one. Digests are listed in `scripts/_kernel/policies/container-images.json`. Installing dependencies first (`--install`) needs a separate grant for `install-dependencies`; ask the user. |
| `result reclassify RES STATUS --reason R --evidence EV...` | Change a non-passing status to another non-passing one, or `passed` to `inconclusive`, citing evidence. Nothing is ever upgraded to `passed`. |
| `compare BASE HEAD` | Test-by-test difference between two recorded runs. |
| `state show [KEY]`, `ledger show`, `ledger verify` | Read the state and the ledger; check that every chain and evidence file is intact. |
| `report check FILE` | Check that every claim in a written report cites a recorded run. |

## Project tools and how their presence is checked

| Tool | Needed for | Presence |
|---|---|---|
| `python3` 3.9+ | The kernel itself | Running `qa doctor` at all |
| `git` | Recording the commit a run saw | `qa doctor` environment tools |
| `pytest` | Python suites | Run it through `qa run`; a missing module is `blocked` / `environment_error` |
| `node`, `npx`, project `node_modules` | Jest, Vitest, Playwright, Cypress | `qa doctor`; a missing package is `blocked` |
| `java`, `./gradlew`, `mvn` | JVM suites | `qa doctor` shows `java`, `gradle`; the wrapper may need a download, which is `blocked` offline |
| `go`, `cargo` | go and Rust suites | Run through `qa run`; missing is `tool_missing` |
| `docker` | Container runs for untrusted projects | `qa doctor` reports the server version; absent means no container run, and an untrusted project gets no test execution |

## Optional converters (only if already installed)

| Tool | For | Command shape |
|---|---|---|
| `jest-junit` | JUnit XML from Jest | `--reporters=default --reporters=jest-junit` |
| `gotestsum` | JUnit XML from go test | `gotestsum --junitfile {evidence}/junit.xml -- ./...` |
| `cargo-nextest` | JUnit XML from cargo | `cargo nextest run` with a JUnit report configured in its profile |

If a converter is not installed, run the plain command, say that the result rests on the exit code alone, and suggest the converter to the user. Do not install it.
