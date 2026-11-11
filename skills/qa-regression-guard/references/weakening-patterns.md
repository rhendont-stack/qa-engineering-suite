# Weakening patterns

`scripts/test_weakening_scan.py` flags each hunk below. A hit is a lead, not a verdict: read the hunk (`git diff <base> <head> -- <path>`), decide whether the change is legitimate, and either record a `test-quality` finding or clear it in the answer with the reason. Contents: the rules, switching a mechanism off, the finding template.

## The rules

| Rule | What the hunk does | Per language (examples of what is flagged) | Legitimate when |
|---|---|---|---|
| `loosened-assertion` | An exact assertion on a value is replaced by a weaker one on the same value | Python `assertEqual(x, v)` to `assertTrue(x)`, `assert x == v` to `assert x`; JS/TS `toBe`/`toEqual` to `toBeTruthy`/`toBeDefined`; Java `assertEquals` to `assertNotNull`/`assertTrue`; Go testify `Equal` to `NotNil`/`True` | Rarely: the value is truly non-deterministic and a property check replaces it (then say which property is still checked) |
| `assertion-removed` | A hunk removes more assertions than it adds, outside a removed test | Any assertion form | The checked behaviour was removed from the product in the same change |
| `deleted-test` | A test definition disappears and no new name replaces it | Python `def test_*`, JS/TS `it(`/`test(`, Go `func Test*`, JVM `@Test` methods, Ruby `it` | The feature it tested is removed in the same change |
| `skip-added` | A test is skipped, marked expected to fail, or focused (`only`, which skips every other test) | Python `@pytest.mark.skip`/`skipif`/`xfail`, `pytest.skip()`, `@unittest.skip`, `skipTest`; JS/TS `it.skip`, `describe.only`, `xit`, `fit`; Java `@Disabled`, `@Ignore`, `assume*`; Go `t.Skip`; C# `Skip=`; PHP `markTestSkipped`; Rust `#[ignore]` | A skip with a tracked reason for an environment the test cannot run in; never to make a red change green |
| `tolerance-widened` | An approximate comparison accepts more error | `approx(abs=, rel=)`, `assertAlmostEqual(places=, delta=)`, `toBeCloseTo(v, digits)`, `within(`/`offset(` | The new tolerance is justified by a documented precision |
| `snapshot-rewritten` | A stored snapshot changed or was deleted | `__snapshots__/`, `*.snap` | The output change is the intended feature, and the review says which part changed |
| `coverage-threshold-lowered` | A coverage minimum goes down | `fail_under`, `--cov-fail-under`, Jest `coverageThreshold`, JaCoCo `minimum`, Codecov `target` | Almost never inside a feature change |
| `ci-failure-ignored` | A pipeline step may fail without failing the pipeline | `continue-on-error: true`, `allow_failure: true`, `soft_fail`, `\|\| true`, `set +e` | A non-blocking informational job, labelled as such |
| `tests-excluded` | The runner is told to leave tests out | `--ignore`, `--deselect`, `collect_ignore`, `-k 'not …'`, `-m 'not …'`, `testPathIgnorePatterns`, Surefire/Gradle `exclude` | Slow suites moved to another job that still runs them (show the job) |
| `runner-hook` | Code the runner calls that can change which tests run or what a result says | pytest hooks in a `conftest.py` or plugin (report, collection and session hooks; assignments to a report's outcome); Jest `reporters`/`testResultsProcessor`; `process.on('exit')`; JUnit extensions and TestNG listeners | Reporting-only hooks that never touch an outcome; read the body |
| `forced-exit` | Test code ends the process with success | `os._exit`, `sys.exit(0)`, `process.exit(0)`, `System.exit(0)`, `os.Exit(0)` | Never in test code |
| `equality-overridden` | An equality method always answers true | Python `__eq__` returning `True`; Java/Kotlin/C# `equals` returning `true` | Never |
| `test-detection-in-product` | Product code checks whether it runs under a test | `PYTEST_CURRENT_TEST`, `"pytest" in sys.modules`, `NODE_ENV === 'test'`, `JEST_WORKER_ID`, `Rails.env.test?`, `testing.Testing()` | Never: the tests then check a different program |
| `special-cased-input` | A product condition compares with a literal that an added test also uses | `if x == <literal>` / `case <literal>:` in product code, the same literal in a new test | The literal is a documented rule (read the requirement), not just the test's input |

A hunk can be suspicious without a hit (a test that now asserts on a mock instead of the result, a fixture that returns the expected value). Read the test files the impact report lists as changed.

## Switching a mechanism off

To show what a mechanism hides, run the head again with it off through the runner's own options, in the head worktree, with the usual `run` form. Never edit a file to do it.

| Mechanism | Option |
|---|---|
| pytest hook in a `conftest.py` | `--noconftest` (every conftest is ignored: a test that needed a fixture from one errors, which is not a product failure; compare by test) |
| pytest plugin | `-p no:<plugin name>` |
| pytest `xfail` | `--runxfail` |
| pytest `--ignore`/`--deselect`/`-k` in configuration | name the excluded test files or IDs on the command line; `-o addopts=""` clears configured options |
| Jest reporter | `--reporters=default` plus the JUnit reporter |
| Jest `testPathIgnorePatterns` | `--testPathIgnorePatterns='^$'` or the path given directly |
| JUnit `@Disabled` | `-Djunit.jupiter.conditions.deactivate=org.junit.*DisabledCondition` |

Then `compare <head EXE> <head EXE with the mechanism off>`: a test in `new_failures` is one the mechanism hides. When no option can switch it off, the finding stays `suspected` and the answer says why.

## Finding template

Write it with the Write tool under `.qa/work/`, then `finding add --json` it. Placeholders in angle brackets.

```json
{
  "title": "<what the change does to the suite, under 120 characters>",
  "description": "<what is weakened or hidden, and what it no longer catches>",
  "category": "test-quality",
  "severity_rationale": {"anchor": "<FUNC-H1 when a recorded run shows a failure it hides; FUNC-M1 otherwise>", "modifiers": []},
  "component": {"path": "<the file holding the hunk>", "criticality": "<high|medium|low>"},
  "verification_status": "<confirmed only with a recorded run that shows the effect; suspected otherwise>",
  "evidence": ["<EXE id of that run>", "<hunk header, e.g. @@ -40,6 +40,7 @@>", "<path>:<line>", "removed: <line>", "added: <line>"],
  "reproduction_steps": ["<git diff <base> <head> -- <path>>", "<the run that shows it>"],
  "impact": "<what can now break without a failing test>",
  "probable_cause": "<path>:<line> <one sentence>",
  "recommendation": "<what the author should restore; nothing was changed here>",
  "refs": {"rule": "<the scan rule, or manual>", "location": "<path, no line number>"},
  "key_attributes": {"rule": "<the scan rule>", "hunk": "<hunk header>"}
}
```

- `refs.location` never ends in a line number; the line goes in `evidence` and `probable_cause`.
- Two findings in one file need different `key_attributes` (the rule and the hunk header), or the second is taken as a new sighting of the first.
- A regression from `compare` is a `functional` finding: the same fields, `category` `functional`, an anchor from the FUNC column for what the failing test guards, `key_attributes` `{"test": "<test ID>"}`, and both EXE IDs in `evidence`.
- `confirmed` needs an EXE whose strength is `exit_code` or `structured` in `evidence`; the kernel refuses it otherwise.
