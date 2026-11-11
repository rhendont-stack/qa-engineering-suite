# Conventions

Follow the project's own framework and style. Add no framework, plugin or dependency without the user's approval; install nothing.

## Where a test goes

- A new file in the project's test directory, inside a `write-tests` grant's `paths`. Name it after the behaviour, in the project's pattern (`test_<topic>.py` for pytest).
- Never append to an existing test file: that is a modification of a tracked file, which needs the user's words for that file (`changes record --kind modified --justification --approval`).
- Without a grant covering the path, the same file goes into a patch under `.qa/work/patches/` (SKILL.md step 4).

## Tags: how a test is tied to its case

`trace scan` links a test to a case from a tag in the test's name: `TC` then an optional `-` or `_` then the digits, uppercase, not glued to a letter or digit before it. It reads names, not docstrings or comments.

| Where | Tagged name |
|---|---|
| pytest function | `def test_TC_012_rejects_a_reused_password():` |
| Other frameworks | the tag at the start of the test's name as the framework's report records it, for example `TC-012 rejects a reused password` |

`test_tc_012_...` (lowercase) is not a tag. For a language whose test ids the kernel does not derive, `trace scan` lists the file as unmapped; map it in `.qa/trace/test-map.json` only through the kernel's documented form, and say so.

## One test, one case

- One test function per case. Several inputs of one case may be one parametrized test.
- The test name says what is checked, after the tag: `test_TC_012_rejects_a_reused_password`, not `test_TC_012`.
- Import the code under test the way the existing tests do.

## Deterministic tests

- Fixed inputs. No random data without a fixed seed.
- Fixed time: pass the moment in, or use the project's clock fixture; never compare against the real current time.
- No network, no shared services, no sleeps. A test that needs a service is a case for another level; say so.
- No order dependence between tests; build what each test needs inside it or in a fixture.

## Assertions

- Assert the exact expected value the oracle gives (`assert result == expected`), not a weaker property (`is not None`, `> 0`, truthiness) unless the criterion states only that.
- For floats, compare with a tolerance the criterion allows (`pytest.approx`) and state it.
- Never wrap an assertion in `try/except`, never mark a test `skip` or `xfail` to keep a suite passing, never add a conftest hook that changes outcomes.

## Run commands

Always through the launcher's `run` (SKILL.md Tools), with a report the kernel can read, written under `{evidence}`:

| Framework | After `run --report 'junit:{evidence}/junit.xml' --` |
|---|---|
| pytest | `python -m pytest '--junitxml={evidence}/junit.xml' <test file>` |
| Others | the framework's own JUnit report option, pointed at `{evidence}/junit.xml`, and the test file; without a per-test report, a run cannot serve as a red check |

A run whose command does not write the declared report ends `inconclusive` / `report_missing`: fix the flag, not the status.
