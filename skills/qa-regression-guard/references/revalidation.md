# Revalidation

`revalidate` turns a fix record into an RVL record. The outcome is derived by the kernel from the recorded runs alone, and the state recomputes it, so a hand-written outcome verifies nothing. Contents: the inputs, the derivation, reading the reason.

## Inputs

- A fix record from `fix add --json FILE` with `{"findings": [...], "base_commit": "<40 hex>", "fix_commit": "<40 hex>", "description": "..."}` and an optional `ref`. Its findings must be `open`, `reopened` or `fix_submitted`; they become `fix_submitted`.
- `--f2p BASE:HEAD`: the run of the defect tests at `base_commit`, then at `fix_commit`.
- `--p2p BASE:HEAD`: the regression-set runs at the same two commits. The full-suite runs of the base and head worktrees serve for both pairs.
- `--defect-test ID`, once per defect test: the tests that show the defect. `scripts/fail_to_pass.py` proposes only the recorded tests the fix's findings name (`key_attributes.test`, or the evidence) that failed by assertion on the base run. Then its `status` is `proposed` and it prints the full command: run it as printed.
- When the findings name no such test, `fail_to_pass.py` prints `status` `unverified`, no command, and exits 1. A test that merely failed on the base run, or that the fix changed, is not evidence about the finding: it may be unrelated, flaky or loosened by the fix. The fix is unverified until a test that shows the defect at the base is named in the finding. Say so in the answer; never pass a `--defect-test` you chose yourself.

The kernel refuses (nothing recorded) when an execution is unknown, is not at the fix's base or fix commit (`commit_mismatch`), or chains attempts across commits.

## The derivation

First match wins:

1. Any of the runs on a dirty tree: `inconclusive`. Runs in suite worktrees are clean.
2. A definite failure: `failed`, and the findings go to `reopened`.
   - FAIL_TO_PASS: a defect test passed on the base (it does not show the defect), or still fails by assertion on the fix.
   - PASS_TO_PASS: a test outside the defect tests passed on the base and fails on the fix, is gone from the fix run, fails on both, or exists only on the fix and fails there.
3. Anything that concludes nothing: `inconclusive`, and the findings stay `fix_submitted`. A defect test absent, erroring instead of failing by assertion, flaky, not run or blocked; no test passed on both commits.
4. Otherwise `passed`, and the findings go to `verified_fixed`.

## Reading the reason

| `reason` says | Meaning | What to do |
|---|---|---|
| dirty tree | A run saw uncommitted changes | Re-run in suite worktrees |
| defect test … absent on the base | The test was added with the fix | It cannot show the defect at the base; a test that exists at the base is needed (test generation) |
| did not fail by assertion | An import or fixture error, not the defect | Report it; the fix is unverified |
| passed on the base | The test does not reproduce the defect | Choose the test that fails at the base, or hand off for one |
| no regression evidence | No other test passed on both commits | Run the full suite at both commits |

Report the outcome and the reason as recorded, with the RVL ID. A `failed` or `inconclusive` revalidation is the answer, not a step to retry with other inputs until it passes.
