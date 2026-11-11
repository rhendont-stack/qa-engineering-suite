# Red check

A test that has never been seen failing may not be able to fail: a wrong import path that tests another function, an assertion on a value the code cannot change, a fixture that short-circuits the call. The red check is the recorded proof that the test fails when the behaviour it checks is broken. The kernel accepts `trace link TC-nnn red_check EXE-...` only when that execution holds a `failed` result with `failure_kind: assertion` for a test the case is `automated_by`. An error (missing module, bad fixture, syntax) is refused: it shows the test is broken, not that it can catch a defect.

Every launcher call below is written in full, as SKILL.md Tools shows. Run `trace scan` before linking, so the case is `automated_by` its test.

## Kind 1: failing on the current commit

When the new test fails on an assertion in its first run on HEAD, the product disagrees with the criterion. That run is the red check:

`python3 .claude/skills/qa-test-generation/scripts/qa.py --role builder --skill qa-test-generation trace link TC-nnn red_check EXE-...`

The test stays as written and stays red. Record the defect as a finding (SKILL.md step 9). For a regression test of a known defect this is the expected path: it fails on the commit with the defect and passes once the owner's fix lands.

## Kind 2: a targeted mutant in the suite's worktree

When the new test passes on HEAD, break the behaviour it checks in a copy of the code that is not the user's working tree:

1. `python3 .claude/skills/qa-test-generation/scripts/qa.py --role builder --skill qa-test-generation worktree add HEAD` prints `.qa/work/wt-<hex>`, a checkout of HEAD owned by the suite. The user's files are never touched.
2. The new test file is not committed, so the worktree does not have it. Write the same content to `.qa/work/wt-<hex>/<same relative path>` with the Write tool.
3. In the worktree's copy of the code under test, make one small change per test that breaks exactly the behaviour that test checks. Good mutants:
   - negate a condition the rule depends on;
   - return a constant, or the input unchanged, where the rule computes something;
   - drop one validation branch;
   - swap two fields, or two arguments of a call;
   - change a stated constant to another value.
   A mutant that breaks everything (an exception on import, an empty module) is not targeted: the test then fails with an error, which is no red check.
4. Run the new tests there, in one Bash command:
   `cd .qa/work/wt-<hex> && python3 ../../../.claude/skills/qa-test-generation/scripts/qa.py --role builder --skill qa-test-generation run --report 'junit:{evidence}/junit.xml' -- python -m pytest '--junitxml={evidence}/junit.xml' <test file>`
   The next Bash call starts from the project root again: write it as usual, with no `cd`. Test ids are the same as in the project, because the paths are relative.
5. Link each test that failed on an assertion: `python3 .claude/skills/qa-test-generation/scripts/qa.py --role builder --skill qa-test-generation trace link TC-nnn red_check EXE-...`.
6. A test that still passed: its mutant did not reach what it checks, or the test checks nothing. Change that mutant (Edit in the worktree) and run again; if no reasonable mutant fails it, rewrite the test from the oracle or report it `unproven`.
7. `python3 .claude/skills/qa-test-generation/scripts/qa.py --role builder --skill qa-test-generation worktree remove .qa/work/wt-<hex>`.

Several mutants may sit in the worktree at once when each test checks a different behaviour: one run then gives every test its red check. Mutants never leave `.qa/work/`; the project's own files are not edited, not even "for a moment".

## What to report

For each kept test: the execution where it passed on HEAD (or failed, kind 1), and its red check execution with the mutant in one line ("condition negated in `<function>`"). A test with no red check is listed as `unproven` and is not counted as verification.
