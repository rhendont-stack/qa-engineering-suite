# Tools

This skill installs nothing. A missing tool makes a run `blocked`: report what is missing and what the user could install. Every kernel call is written out in full. From the project root:

`python3 .claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation <command>`

Inside a suite worktree:

`cd <root>/.qa/work/wt-<hex> && python3 ../../../.claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation <command>`

`<root>` is the absolute path of the project root. The `cd` stays in effect for later Bash calls, so go back with `cd <root> && …`.

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show`, `config set project.trust trusted --approval "<the user's words>"` | The workspace, and the user's approval to run the project's code here |
| `worktree add <commit>`, `worktree remove <path>` | A suite worktree at `.qa/work/wt-<12 hex>` that holds exactly that commit. A run started inside it is recorded at that commit, and nothing it writes reaches the user's checkout |
| `run [--report FORMAT:PATH] [--timeout S] -- <command>` | One recorded run. Exit codes: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run. With a `cobertura`, `lcov`, `jacoco` or `mutation` report, the printed JSON holds `measurements` with the totals |
| `run --container IMAGE@DIGEST -- <command>` | The same run in a container with no network, from an archive of the project root. Refused inside a worktree |
| `ingest mutation FILE --from EXE` | Attaches a mutation-testing-report-schema file to the run that produced the mutants. The kernel computes the score; it never takes a tool's percentage |
| `finding add --json FILE` | One `test-quality` finding |
| `state show [KEY]` | The state, read through the kernel (`state show executions` lists the runs with their measurements and ingested reports) |

## Python: mutmut 3.x

mutmut copies the code under test into `mutants/` in the directory where it runs, rewrites it, and runs pytest there against each mutant. Run it only inside a suite worktree. Run in the user's checkout, it would leave `mutants/`, a config file and caches in their working tree.

Configure it in the worktree's own `setup.cfg`, written with the Write tool. If the worktree already has a `setup.cfg`, add the section to it with the Edit tool. Never touch the root's copy.

```ini
[mutmut]
source_paths = <package or directory under test>/
pytest_add_cli_args_test_selection = <test file or directory that exercises it>
also_copy = pytest.ini
```

- `also_copy` lists the test-runner configuration at the project root that the tests need: `pytest.ini`, `conftest.py`, `tox.ini` or `pyproject.toml`, one per line, indented on the lines after the first. Without it, pytest running inside `mutants/` finds the worktree's own configuration one level up. That configuration puts the worktree, with the unmutated code, on the import path, so every mutant "survives" or mutmut reports that no test reaches any mutant. That result is about the setup, not the tests.
- `source_paths` takes directories. mutmut mutates every file in them, so a file outside the question still gets its own line in the report. Quote the score of the file asked about from `by_file`, and the overall score next to it.
- The run, with the conversion inside it: `cd <root>/.qa/work/wt-<hex> && python3 ../../../.claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation run --timeout 900 --report 'mutation:{evidence}/mutation-report.json' --shell -- 'mutmut run && python3 ../../../.claude/skills/qa-mutation/scripts/mutmut_to_report.py --root . --out {evidence}/mutation-report.json'`. mutmut exits 0 whatever survives. A non-zero exit means mutmut itself failed (for example, "failed to run the clean tests": the baseline is not green for the selection), and the converter does not run.
- Then, from the root: `ingest mutation .qa/evidence/<EXE>/reports/mutation-report.json --from <EXE>`. Its `strength` is `structured`.
- Status table, mutmut 3.8.1 → schema: killed → Killed; survived → Survived; no tests → NoCoverage; timeout → Timeout; caught by type check → CompileError; segfault and suspicious → RuntimeError; skipped → Ignored; interrupted and not checked → Pending. `scripts/mutmut_to_report.py` applies it and places each mutant on the line it changes.
- Read a mutant's change: `cd <root>/.qa/work/wt-<hex> && mutmut show <mutant name>`. The stored report already gives each mutant's line and its "before -> after" in `description`.

## JVM: PIT 1.x

PIT mutates bytecode in memory and changes no source file. It needs the project's build (Gradle `info.solidsoft.pitest` or Maven `pitest-maven`) with `outputFormats` set to `XML`. Without that setting, PIT writes HTML only, and HTML is not converted.

- Run it in a suite worktree on the host, with the conversion in the same run, as for mutmut: `cd <root>/.qa/work/wt-<hex> && python3 ../../../.claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation run --timeout 1800 --report 'mutation:{evidence}/mutation-report.json' --shell -- './gradlew pitest --offline && python3 ../../../.claude/skills/qa-mutation/scripts/pit_to_report.py --xml build/reports/pitest/mutations.xml --out {evidence}/mutation-report.json --source-root src/main/java'`. For Maven, `mvn -o org.pitest:pitest-maven:mutationCoverage` and `--xml target/pit-reports/mutations.xml`. The worktree is the isolation: `build/` and `target/` are written there, never in the user's checkout. It needs a JDK and the build's dependencies in the offline cache. If they are not there, the run fails: report it.
- Then, from the root: `ingest mutation .qa/evidence/<EXE>/reports/mutation-report.json --from <EXE>`: `structured`.
- Not in a container. The kernel keeps in a run's evidence only the files a declared report names, and it has no format for PIT's XML, so an XML copied to `{evidence}` is removed when the run is recorded, and a later conversion has nothing to read. A score converted from a copy taken any other way is `attested`. Without a host JDK, report that no score can be recorded, and why.
- Status table, PIT 1.30.2 → schema: KILLED → Killed; SURVIVED → Survived; NO_COVERAGE → NoCoverage; TIMED_OUT → Timeout; NON_VIABLE → CompileError; MEMORY_ERROR and RUN_ERROR → RuntimeError; NOT_STARTED and STARTED → Pending; EQUIVALENT → Ignored.
- PIT's own "mutation score" counts NON_VIABLE, MEMORY_ERROR and RUN_ERROR as detected; the kernel's score leaves them out. Quote the kernel's. PIT 1.30.2 writes `partial="true"` on the root element even for a complete run, so judge completeness from the counts: Pending is 0 when every mutant got a verdict.

## JavaScript and TypeScript: StrykerJS

StrykerJS works in a sandbox copy and writes mutation-testing-report-schema JSON itself (`reporters: ["json"]`, file `reports/mutation/mutation.json`), so no converter is needed. Run it in the pinned Node image, copying the JSON into the run's evidence and declaring it as the run's report, so the kernel keeps it: `python3 .claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation run --container <node image>@sha256:<digest> --report 'mutation:{evidence}/mutation.json' --shell -- 'npx stryker run && cp reports/mutation/mutation.json {evidence}/mutation.json'`. `node_modules` with Stryker must already be in the project. Then `ingest mutation .qa/evidence/<EXE>/reports/mutation.json --from <EXE>`: `structured`.

## Other languages

cosmic-ray (Python), cargo-mutants (Rust), Infection (PHP), Stryker.NET and gremlins (Go) are not converted by this skill. If such a tool writes mutation-testing-report-schema JSON, ingest it. Otherwise report that no score can be recorded for that language, and why. Never transcribe a tool's printed percentage.

## Coverage beside the score

Coverage is measured in the same worktree on the same test selection, as two recorded runs:

1. `cd <root>/.qa/work/wt-<hex> && python3 ../../../.claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation run -- python -m coverage run --branch --source=<package> -m pytest -p no:cacheprovider -q <tests>`
2. `cd <root>/.qa/work/wt-<hex> && python3 ../../../.claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation run --report 'cobertura:{evidence}/coverage.xml' -- python -m coverage xml --include=<file asked about> -o {evidence}/coverage.xml`

The second run prints `measurements` with `lines_covered`/`lines_total` and `branches_covered`/`branches_total` for the included file. Line coverage is `lines_covered / lines_total`. For JaCoCo, ingest `build/reports/jacoco/test/jacocoTestReport.xml` as `jacoco`. For Jest or Vitest, ingest `coverage/lcov.info` as `lcov`.

## The report the converters write

mutation-testing-report-schema 1.0: `{"schemaVersion": "1.0", "thresholds": {"high": 80, "low": 60}, "files": {"<path>": {"language", "source", "mutants": [{"id", "mutatorName", "status", "location": {"start": {"line", "column"}, "end": {…}}, "description", "statusReason", "killedBy"?}]}}}`. Paths are relative to the project root. The converters write the file only after the kernel's own parser has accepted it.

They run inside a kernel run that declares their output as its `mutation` report, so the kernel stores the file under `.qa/evidence/<EXE>/reports/`, lists it in that execution's manifest and prints its totals as the run's `measurements`. `ingest` of that stored file is `structured`, and only a structured ingest (or the run's own measurement) is a score: a `mutation_score_min` gate reads nothing else. The same converter run outside the kernel writes a file the kernel cannot tell from one a person wrote: `attested`, never quoted as a score. The converters also print `statuses`, `score`, `by_file` and `undetected` into the run's `stdout.log`; no number is quoted until `ingest` has recorded the file as `structured`. The kernel's formula is 100 × (Killed + Timeout) / (Killed + Timeout + Survived + NoCoverage). The test strength over covered mutants only is the same formula without NoCoverage. Quote it as "covered-mutant score", never as the score.
