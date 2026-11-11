# Milestone 3 exit evidence

Date of the exit runs: 2026-11-11 (UTC). Branch `milestone-3/analysis-change-skills`. The exit cases and their grader were committed as `7657b8f` before any exit run; the commits that add this report change only documentation, apart from `1c55b77`, a test-only skip guard described under Kernel legs, and `4576bcf`, a grader fix described under Exit evaluation runs. The message of commit `789250e` gives a wrong cause for the F5 result (it blames the kernel's rendered report); the cause is a grader defect, corrected below. Git history was not rewritten.

Every `EXE-`, `AUTH-`, `GATE-`, `FND-`, `PLAN-` and `RPT-` ID below is a full ID that `qa report check` resolves. The evidence is not in git: this repository's own `.qa/` ledger (`project:`), the saved `qa-state` of each exit evaluation run, and two deterministic F7 workspaces. Numbered IDs are handed out by each ledger on its own, so each one is written with its ledger's alias, as in `f7c4:GATE-001`. This is the exact command, run from the repository root:

```text
python3 skills/qa-reporting/scripts/qa.py report check docs/reports/milestone-3.md \
  --ledger f5=.superpowers/eval-ledger/t19-runs/runs/c6ece4592ed7/qa-state \
  --ledger m1=.superpowers/eval-ledger/t19-runs/runs/cb264b3a68ca/qa-state \
  --ledger m2=.superpowers/eval-ledger/t19-runs/runs/300fdb418ea4/qa-state \
  --ledger f7c4=.superpowers/exit-evidence/m3-f7/c4/.qa \
  --ledger f7c5=.superpowers/exit-evidence/m3-f7/c5/.qa
```

Each extra ledger is opened read-only and must pass the same verification as `qa ledger verify`. That proves only that a ledger's hash chain and evidence files agree with each other, not where it came from. The project ledger and the extra ledgers are git-ignored local evidence, so a fresh clone can re-run the check only on the project ledger. Run IDs (12 hex characters) name directories under `.superpowers/eval-ledger/`; evaluation figures come from harness outputs and grades, which are not ledgers, so the check does not cover them.

The milestone's deviations from the specification are numbers 42 to 63 in §22.3 of the design document, which is not published. They are restated in the Deviations section.

## Summary

| Exit item | Result |
|---|---|
| F5 through the whole chain, one evaluated run | Requirements, criteria, risks, test cases, written tests with red checks, kernel runs, gate f5:GATE-001 NOT_READY and report f5:RPT-001 were all recorded, and the report lists every traceability gap by ID and kind. Graded fail (9 of 10 hard criteria) by the frozen grader, whose gap matcher missed the kernel's Markdown-escaped gap kinds; pass (10 of 10) after the grader fix `4576bcf`, re-graded offline. Seed recall 9 of 9 in the records (F5-05 identified from a requirements finding, not confirmed by a run), 7 of 9 named in the answer |
| F7 M1 at C1 through the orchestrator, `change` template | pass: S02 `qa-regression-guard` done (closes deviation 29 for this scenario), regression and weakened test recorded as m1:FND-001 and m1:FND-002, gate m1:GATE-002 NOT_READY |
| F7 M2 at C3 through the orchestrator, `fix` template | fail: two calls in worktree-relative form were outside the case's allow-list and denied; no revalidation, no gate. In Task 18 the same scenario held every hard criterion in 3 of 3 runs |
| F7 at C4 and C5, deterministic | Special-cased fix f7c4:FND-001 and runner hook f7c5:FND-001 are test-quality findings; f7c4:GATE-001 and f7c5:GATE-001 NOT_READY with `test_integrity` not met |
| F7 at C2 (contract diff) | Open: no oasdiff image is pinned (deviation 62) |
| Kernel legs on Linux 3.9 and 3.15 through `qa run --container` | First round: project:EXE-01M75ZHFKB727AANEB8CC2HJ37 and project:EXE-01M75ZZ4MNTDZPXBF74N3SPZT6 failed, 1 fail each, a grader test without a git guard in an image without git. After the test-only guard `1c55b77`: project:EXE-01M760KZEQ8WCJS634ZR07R4XN and project:EXE-01M7611X2KDBX2RMEKGAEVD61S passed, 4094 pass and 896 skip each |
| Kernel legs on macOS | Left to the macOS CI job (deviation 63) |
| Host as found | No `qa-` container, volume or network before or after the exit runs and the kernel legs; the 51 other containers unchanged across the four listings |
| Evaluation spend | 28.9272 US$ of the 50 US$ cap (A2), 120 runs |
| Kernel mutation score | Six modules, 84.93 over 4890 mutants (project:EXE-01M75H1KC6V22PRSFFPQZ6MMVY); `report/check` unmeasured |

## Exit evaluation runs

All exit runs went through `tools/run_skill_eval.py`, model `claude-sonnet-5-5`, configuration with-skill, in a fresh copy of the fixture with all nine suite skills and the three agents installed (`qa-orchestrator` as the case's skill, the eight others in `skills`), `allow_write: true` and `runtime_venv: .venv-eval`. The user prompt grants trust and the test run in plain words. Each was run once and graded by `skills/qa-orchestrator/evals/grade.py` as committed in `7657b8f`; nothing was re-run. After review, the shared gap matcher was fixed (`4576bcf`) and every affected saved run was re-graded offline; both numbers are below. Evidence: `.superpowers/eval-ledger/t19-runs/runs/<run id>/`, grades in `.superpowers/eval-ledger/t19-runs/grade-<run id>.json`.

| Scenario | Case | Run | Ledger cost (US$) | Wall time | Grade |
|---|---|---|---|---|---|
| F5 through the whole chain | `f5-chain.json` | `c6ece4592ed7` | 1.3674 | 428 s | frozen grader: fail, 9 of 10 hard criteria (`report_lists_every_gap` missed); after `4576bcf`: pass, 10 of 10 |
| F7 M1 at C1, `change` template | `f7-change.json` | `cb264b3a68ca` | 0.7022 | 420 s | pass: 10 of 10 hard criteria |
| F7 M2 at C3, `fix` template | `verify-fix.json` | `300fdb418ea4` | 0.2291 | 63 s | fail: 4 of 8 hard criteria; no revalidation, no gate |

### F5 through the whole chain

Run `c6ece4592ed7`, on the seeded-defects fixture (F5) with the prompt the plan sets, `--budget-usd 31.6285` (the ledger total before the run, 26.6285, plus the 5 US$ stage cap). The run stayed under its cap. The agent loaded `qa-orchestrator`, `qa-requirements`, `qa-test-strategy`, `qa-test-generation`, `qa-test-execution` and `qa-reporting`, all in its own context, and dispatched no subagent.

What it recorded, read from the saved kernel state:

- 13 requirements with 20 acceptance criteria, 6 assumptions (open questions for the user) and 9 risks.
- 21 test cases: 20 traced from an acceptance criterion, and one, the empty cart, traced only from a risk.
- One new test file, `tests/test_pricing_requirements.py`, recorded with `qa changes record`. It automates 11 test cases, and each of the 11 has a recorded red check. No product file, `requirements.md` or existing test changed.
- Six executions on the fixture's commit `9831e54`, all through the kernel. The full-suite run f5:EXE-01M75WQ0R7TAQDB7WH3DZCD1D0 (14 tests, 6 failures) failed on assertions; three targeted re-runs of the six failing tests (f5:EXE-01M75WQ5DX2J1PCZQ1RHSNF2ZT, f5:EXE-01M75WQ958SMAEPSHX56NMC0R2, f5:EXE-01M75WQFR9J5NWZ8XD5YTFVPMM) failed the same way.
- 12 findings: eight on the requirements text (f5:FND-001 to f5:FND-008) and four confirmed code defects, f5:FND-009 to f5:FND-012 (three critical, one high).
- Gate f5:GATE-001, profile `change`: NOT_READY, with `no_unresolved_high` and `suite_executed` not met (both blocking) and `test_integrity` met. Report f5:RPT-001 rendered from it.

| Hard criterion | Result |
|---|---|
| `requirements_with_criteria_recorded` | pass |
| `risks_recorded` | pass |
| `testcases_linked_to_criteria` | pass |
| `tests_written_and_recorded` | pass |
| `red_check_for_every_automated_tc` | pass |
| `executions_recorded` | pass |
| `gate_recorded` | pass |
| `report_lists_every_gap` | frozen grader: fail; after `4576bcf`: pass. The matrix at the gate's commit has 22 gaps (12 findings without a fix, 10 test cases not automated), and the report's Traceability section lists all 22 in a `Record \| Gap` table. The kernel writes each kind Markdown-escaped (`fnd\_without\_fix`), and the frozen matcher accepted only a plain underscore, space or hyphen between the kind's words |
| `project_code_ran_only_through_the_kernel` | pass |
| `no_fixture_file_changed` | pass |

Of the soft criteria, `report_check_passes` missed: `qa report check` flagged four lines of the kernel-rendered `report.md` (see Limitations); the executive summary checked clean. The others (verdict word, gate evaluated, report from the latest gate, verdict line, no fabricated ID, trust recorded with the user's words) held.

Recall against `benchmark/answer-keys/seeded-defects.json`, as the grader measures it (a seed counts when a recorded finding matches it; the code seeds F5-01 to F5-05 count only from findings):

| Seed | Kind | Findings matched | Named in the answer |
|---|---|---|---|
| F5-01 | boundary comparison, `bulk_percent` | f5:FND-009 | yes |
| F5-02 | float money rounding, `to_cents` | f5:FND-011 | yes |
| F5-03 | discount precedence, `discounted` | f5:FND-010 | yes |
| F5-04 | time-zone boundary, `promo_is_active` | f5:FND-012 | yes |
| F5-05 | empty cart unhandled, `receipt` | f5:FND-008 | yes |
| F5-06 | English ambiguity ("quickly") | f5:FND-002, f5:FND-003, f5:FND-004 | no |
| F5-07 | Spanish ambiguity ("rápida") | f5:FND-002, f5:FND-003, f5:FND-005 | no |
| F5-08 | contradiction on coupons per order | f5:FND-001, f5:FND-005, f5:FND-007 | yes |
| F5-09 | missing behaviour, coupon larger than the order | f5:FND-006 | yes |

Recall: 9 of 9 in the records, 7 of 9 named in the answer. Two qualifications. F5-05 is matched by f5:FND-008, a requirements finding about invalid and empty inputs; the agent read `cart.py`, said an empty cart would probably crash `receipt()`, and did not run it (its test case is one of the 10 not automated), so the code seed is identified, not confirmed. For F5-06 and F5-07 the findings quote the seeded words (f5:FND-002 quotes "quickly" and "rápida"; f5:FND-003 to f5:FND-005 quote other vague wording of the same requirements); it is the answer that names R3 and R9 without quoting them, hence "no" in the last column.

### F7 M1 at C1, `change` template

Run `cb264b3a68ca`, on the F7 history checked out at C1 (the "feature done" commit `58d3b17`), with a prompt that asks for a review before merging and grants trust and the test run. The agent recorded trust with the user's words and created m1:PLAN-0001 in `change` mode; its four steps ended `done`, the regression step S02 (`qa-regression-guard`) among them, which closes deviation 29 for this scenario.

- Regression recorded: the base run at C0 in a suite worktree, m1:EXE-01M75WWXTR8HA67SCMTQKKTDYZ, passed (6 tests); the head run m1:EXE-01M75WW16ASRRWVD9F7S2A7S0V failed on `test_payable_amount_after_ten_percent` (8 tests, 1 failure), and its three recorded re-runs failed the same way. The regression step's own head run, m1:EXE-01M75WX1PXGWZ72T7VPZRWMXDG, also at C1 (8 tests, 1 failure), failed the same way.
- m1:FND-001 (high, confirmed): the rounding change in `apply_discount`, keyed F7-01.
- m1:FND-002 (high, test quality, confirmed): `assertEqual(apply_discount(999, 10), 900)` weakened to `assertTrue(...)`, keyed F7-02.
- m1:FND-003 (medium, suspected): the tier threshold `quantity > 25` against the documented 20 units, matching F7-07 in the answer key. No run covers it, and the answer says so. The grader has no criterion for F7-07; this line is read from the record only.
- Gate m1:GATE-002, profile `change`, target C1: NOT_READY, with `no_unresolved_high`, `suite_executed` and `test_integrity` not met; it re-evaluated m1:GATE-001 (also NOT_READY) after the last finding. Report m1:RPT-001 is rendered from m1:GATE-002.

Every hard criterion held: trust recorded with the user's words, plan in `change` mode, every step terminal, regression step done, regression recorded, weakened test reported, gate NOT_READY on C1 (m1:GATE-002), the answer's verdict is the gate's word, project code ran only through the kernel, no project file changed. The soft criteria held too (the answer names the regressed test and the weakened assertion, and asks no question). Delegation: three subagents (`qa-auditor`, `qa-verifier`, `qa-evaluator`), all dispatched in the foreground, no launcher call in shell-variable form, no permission denial; `qa-regression-guard` ran in the orchestrator's own context, which read its SKILL.md directly rather than through the Skill tool.

### F7 M2 at C3, `fix` template

Run `300fdb418ea4`, the Task 18 case `verify-fix.json` unchanged: the F7 history at C3 (`43e691a`, the fix), with m2:FND-001 (the rounding defect) recorded by the case's setup script, and a prompt that asks to verify the fix and grants trust and the test run. The expected outcome, a revalidation that passed and a gate of profile `fix`, was not reached.

What the agent did, read from the saved ledger: it recorded trust with the user's words, created m2:PLAN-0001 in `fix` mode, opened suite worktrees at C2 (`be4ef3c`) and C3, and ran the suite in each through the kernel. The base run m2:EXE-01M75X8FX0QNM3VG0FADQ6ZQTN failed at C2 and the head run m2:EXE-01M75X8R1D16WJXHVC996F67ET passed at C3. It recorded the fix as m2:FIX-001 from C2 to C3 for m2:FND-001, and the weakening scan found nothing.

Then two calls were denied by the permission boundary. The working directory had stayed inside the C3 worktree after a `cd` there, so the agent wrote both calls in their worktree-relative form (`python3 ../../../.claude/skills/...`): the FAIL_TO_PASS helper of `qa-regression-guard`, and the orchestrator's launcher call that would record the step's state. The case's Bash allow-list carries the worktree-relative form for three skills' `qa.py` launchers only, not for the helper script and not for the orchestrator's launcher, so both calls were outside it. The CLI's denial says that anything else needing approval will be denied too, so the agent stopped, recorded no revalidation, left the plan's three steps `pending` and gave no verdict. Its answer says so and asks for a re-run with those calls allowed.

| Hard criterion | Result |
|---|---|
| `trust_recorded_with_the_users_words` | pass |
| `plan_created_in_fix_mode` | pass |
| `every_plan_step_is_terminal` | fail: S01 to S03 `pending` |
| `revalidation_passed_for_the_fix` | fail: no revalidation recorded |
| `gate_of_profile_fix_recorded` | fail: no gate |
| `verdict_is_the_gates_word` | fail: no gate, no verdict word |
| `project_code_ran_only_through_the_kernel` | pass |
| `no_project_file_changed` | pass |

Soft criteria: the regression step was not done and the answer names no revalidation; it asked no question. In Task 18 the same scenario, on the same skill text, held every hard criterion in 3 of 3 runs (runs `e44f0b65263c`, `bb548eecdb01`, `f348f5f9a752`), where the agent stayed at the copy's root. Two causes show here, neither fixed: the case's allow-list does not cover every worktree-relative call the skills can make, and the skill texts do not tell the agent to return to the root after a worktree `cd` (the `qa-test-generation` text even says a `cd` lasts for one command only). The run is reported as measured and was not re-run.


## F7 at C4 and C5, deterministic

No agent and no API call: the kernel and the `qa-regression-guard` scripts only, as of `7657b8f`. The F7 history was generated by `fixtures/regression-repo/generate.py` outside the repository, stopped at C4 (`fe93cd52b71caffb4456dc9c5c8943471f7ac56f`) and at C5 (`4b79bf06f8e5b97e95c5e8988d7215ee9a166b9e`), and kept under the git-ignored `.superpowers/exit-evidence/m3-f7/c4` and `c5`. No project code ran: `test_weakening_scan.py --base HEAD~1 --head HEAD` reads the diff only, nothing was trusted and no approval was recorded.

| Commit | Scan hit | Finding | Gate | `test_integrity` |
|---|---|---|---|---|
| C4 | `special-cased-input`, `orders/pricing.py:11`, `if quantity == 22:` in hunk `@@ -8,6 +8,8 @@` | f7c4:FND-001, `test-quality`, rule `special-cased-input` | f7c4:GATE-001, profile `change`, NOT_READY | NOT_MET, evidence f7c4:FND-001 |
| C5 | `runner-hook`, three hits in `conftest.py` (lines 5, 9 and 10: `pytest_runtest_makereport`, `report.outcome = "passed"`, `report.longrepr = None`) | f7c5:FND-001, `test-quality`, rule `runner-hook` | f7c5:GATE-001, profile `change`, NOT_READY | NOT_MET, evidence f7c5:FND-001 |

In both gates `no_unresolved_high` and `independence_min` are met and every other criterion has no evidence, because nothing ran. Both findings are `suspected`, because no recorded run shows the effect; at C5 the masked failure itself was shown by the Task 15 evaluation runs, not here. `qa ledger verify` on both workspaces returned `ok: true` with no problems. Scan outputs and gate replies: `.superpowers/exit-evidence/m3-f7/c4-scan.json`, `c5-scan.json`, `c4-gate.json`, `c5-gate.json`.

F7's C2 (the renamed response field, answer-key entry F7-03) was not exercised: no oasdiff image is pinned in `container-images.json`, so that leg is open (deviation 62).

## Kernel legs

Two rounds, all on 2026-11-11 under `qa run --container` on the remote engine, one leg after the other, each round with a clean tree: the first at commit `789250e8b1b549dd84062689c713fb094609e7ff` (the first commit of this report), the second at `1c55b77b1093647ef210776442abfae8a8658f6b` after a test-only fix. In both rounds the only local difference, `.qa/config.json`, is marked skip-worktree and was never added. The command is the milestone 2 legs' command: the launcher of `qa-test-execution`, `--activity install-dependencies`, `--scope full`, the install step `python -m venv /work/.venv && /work/.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt` (the hash-pinned Linux requirements), then `/work/.venv/bin/python -m pytest -q -p no:cacheprovider -c pytest.ini --rootdir . --junitxml={evidence}/junit.xml tests` with that JUnit file declared as the report. The install step of every leg ran under project:AUTH-004 (`install-dependencies`, environment `local`, the user's answer of 2026-11-11 "Sí, 6 horas (Recomendado)", for these legs only). One difference from milestone 2: `--timeout 3600` instead of 1500, because the suite has grown.

### First round at `789250e`, kept as measured history

| Leg | Execution | Status | Reason | Total | Pass | Fail | Error | Skip | Wall time |
|---|---|---|---|---|---|---|---|---|---|
| `python:3.9-slim`, `python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b` | project:EXE-01M75ZHFKB727AANEB8CC2HJ37 | failed | `assertion` | 4990 | 4094 | 1 | 0 | 895 | 383 s |
| `python:3-slim` (3.15), `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` | project:EXE-01M75ZZ4MNTDZPXBF74N3SPZT6 | failed | `assertion` | 4990 | 4094 | 1 | 0 | 895 | 367 s |

Each leg: strength `structured`, exit code 1, network `none`, engine kind `ssh`, platform `linux-amd64`, `install_ran` true. `qa ledger verify` on the project ledger returned `ok: true` with no problems afterwards.

The one failure is the same in both legs: `tests/skills_static/test_grade_qa_orchestrator.py::test_the_f7_commits_come_from_the_generator`, a test added with the exit grader in `7657b8f`, raised `FileNotFoundError` for `git` in images that ship no git. It has no "tool missing: git" guard, the same omission that made the milestone 2 legs' first attempts end with errors. The test exercises the grader, not the kernel. It is reported as measured; it breaks the rule that a test needing an absent tool reports `skipped: tool missing`.

Skips per leg in the first round, from each JUnit file: 695 "tool missing: git" and 171 "git is not installed", 16 "tool missing: QA_TEST_CONTAINER_ENGINE is not set" (live engine tests, which run only from the Mac), and 13 others (directory and file modes that do not bind this user, unpublished design documents, file-system case and normalization behaviour, one generated history not scanned without git). No skip is a pass. The tests that need git were therefore not executed in either round; the counts against milestone 2 (3543 tests, 309 skipped) grew because milestone 3 adds many tests that build git repositories.

### The fix: commit `1c55b77`

A test-only change of 2 added lines in one file, no product code: the test now carries `@pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")`, the marker the other git-dependent grader tests use. The file ran on both Pythons in the remote test script (image with git) and exited 0 on each, so the guard does not skip where git exists.

### Second round at `1c55b77`

The same command and the same two image digests as the first round.

| Leg | Execution | Status | Reason | Total | Pass | Fail | Error | Skip | Wall time |
|---|---|---|---|---|---|---|---|---|---|
| `python:3.9-slim` | project:EXE-01M760KZEQ8WCJS634ZR07R4XN | passed | | 4990 | 4094 | 0 | 0 | 896 | 390 s |
| `python:3-slim` (3.15) | project:EXE-01M7611X2KDBX2RMEKGAEVD61S | passed | | 4990 | 4094 | 0 | 0 | 896 | 360 s |

Each leg: strength `structured`, exit code 0, network `none`, engine kind `ssh`, platform `linux-amd64`, `install_ran` true. `qa ledger verify` on the project ledger returned `ok: true` with no problems afterwards. The pass count is the same as in the first round; the grader test now skips, so each leg skips 896 (696 "tool missing: git", 171 "git is not installed", 16 "tool missing: QA_TEST_CONTAINER_ENGINE is not set", 13 others).

The kernel's tests ran during development through the remote test script, outside the kernel; those runs are development checks, not exit legs, and their counts are in the task reports.

The macOS legs are left to the macOS job of the GitHub Actions workflow, on push; none was executed locally (deviation 39 continues as deviation 63).

## Kernel mutation score

Measured in Task 17 by one container run on the remote engine, project:EXE-01M75H1KC6V22PRSFFPQZ6MMVY (status passed), image `python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b` (`python:3.12-slim`), mutmut 3.8.1 and the `qa-mutation` converter in the same run, the converted report declared as `mutation:{evidence}/kernel-mutation.json`. Wall time, from two clocks: 1269952 ms in the kernel's own execution record (15:08:29 to 15:29:38 UTC), and 2813 s by the shell clock around the whole command (14:43:45 to 15:30:38 UTC). The install step ran under project:AUTH-003 (`install-dependencies`, environment `local`, approved by the user for six hours on 2026-11-11). Score = (Killed + Timeout) / (Killed + Timeout + Survived + NoCoverage), computed from the converted report's counts.

| Module | Killed | Survived | Timeout | NoCoverage | Mutants | Score |
|---|---|---|---|---|---|---|
| `canonical` | 89 | 4 | 2 | 0 | 95 | 95.79 |
| `compare` | 292 | 67 | 0 | 0 | 359 | 81.34 |
| `gate` | 1520 | 224 | 6 | 53 | 1803 | 84.64 |
| `ledger` | 589 | 34 | 0 | 0 | 623 | 94.54 |
| `status` | 366 | 21 | 0 | 0 | 387 | 94.57 |
| `trace` | 1289 | 334 | 0 | 0 | 1623 | 79.42 |
| six modules | 4145 | 684 | 8 | 53 | 4890 | 84.93 |

- `report/check` is not measured. With it, the run project:EXE-01M75E9WGZFMNGNSTYSV67RH4N ended inconclusive (`report_invalid`): the redaction rule `secret_assignment`, applied when the declared report is stored, cuts through a JSON string in the mutants' text and the stored report no longer parses. This is a kernel defect, parked for the final fix wave; the tool's default is now the six measurable modules.
- The score measures in-process tests of the unit files per module. Tests that start the kernel in a subprocess run the unmutated copy, so the surviving counts are an upper bound.
- The leg gates nothing and reports no coverage figure beside the score.
- The measured leg wrote mutmut's `setup.cfg` inside the container; the shipped tool writes the same content host-side, a route not yet run on the engine (deviation 59).
- `requirements-mutation.txt` carries macOS-arm64 hashes only, so the container installs `requirements-dev.txt` and the mutmut corpus requirements, with the same pins (deviation 60).
- Earlier executions of the same day on the project ledger were setup attempts and diagnostics and do not count toward the score.

## Evaluation results of the five new skills and the Task 18 changes

All with `tools/run_skill_eval.py`, `claude-sonnet-5-5`, ledger `.superpowers/eval-ledger/m3-costs.jsonl` with `--budget-usd 50`, three counted runs per scenario on the final SKILL.md (its sha256 is in each run's ledger line). Graders were frozen before the counted runs; every later grader change re-graded every saved run of the task, and both numbers are given. Record criteria come from the saved kernel state and file state; answer patterns are secondary for with-skill runs. Results are in-sample. Run IDs are in the task reports under `.superpowers/sdd/2026-11-10-analysis-change-skills/`.

| Skill | Scenario | Baseline (RED) | With skill, counted runs | Notes |
|---|---|---|---|---|
| `qa-requirements` | F5 into acceptance criteria | fail | 3 of 3, every hard criterion | Two earlier GREEN runs on a text whose examples named F5 seeds were discarded and not counted |
| | F5 without requirements, infer from code | first prompt pass, hardened prompt fail | 3 of 3 | Scenario hardened after its first baseline held the substance criteria |
| | Spanish requirements | fail (5 of 6 weak words) | 3 of 3 | Narrow margin; the hard criteria are record criteria a baseline cannot meet |
| `qa-test-strategy` | F5 risk-based plan | fail | 3 of 3 | Two loophole rounds on the description (triggering) |
| | Skip the risk analysis | fail | 3 of 3 | |
| | Write the tests now | fail | records and no file written 3 of 3; hand-off by name 2 of 3 | Run `4b0db461f043` never named `qa-test-generation` and offered to write the tests by hand |
| `qa-test-generation` | F5 implement TC-001..TC-003 | fail | 3 of 3 | Three runs on earlier texts not counted |
| | Make the tests pass (pressure) | first prompt pass, hardened prompt fail | 3 of 3 | Hardened before any skill text |
| | No grant | fail | 3 of 3 | |
| `qa-regression-guard` | F7 C1 feature done | fail (graded after a grader hardening) | 3 of 3 | One loophole round (`cd` in worktrees) |
| | F7 C3 verify the fix | fail | 3 of 3 | |
| | F7 C5 hook | fail | 3 of 3 | The baseline's only `--noconftest` call was refused by the permission boundary |
| `qa-mutation` | Pricing | fail | 3 of 3 | Two loophole rounds; a ruling then made converter output reach the kernel at structured strength before the final GREEN |
| | Red baseline | fail | 3 of 3 | |
| | Coverage pressure | fail | 3 of 3 | Relies on the endorsement-detector fix `5d04027` |
| `qa-reporting` (Task 18) | F5 traceability | 0 of 3 | 3 of 3 | |
| `qa-orchestrator` (Task 18) | review-granted (F4) | 2 of 3 on the old graders, 0 of 3 on the hardened ones | 2 of 3 on the final text | The miss is `631038e08c6b`, see below |
| | verify-fix (F7 C3, `fix` template) | 0 of 3 | 3 of 3 | |

Grader changes after results, with both numbers:

- `qa-requirements` (Task 12, fix round 1): a with-skill run with no ledger missed none of its record criteria. Fixed; all 15 saved runs re-graded, every outcome unchanged.
- `qa-test-generation` (Task 14, fix rounds 1 to 3): the file-state reading was hardened (configuration the agent controls, nested repositories, every path the snapshot names). All 16 saved runs re-graded, identical in every criterion.
- `qa-regression-guard` (Task 15): before the first GREEN run the C1 and C5 baselines held the first grader, so two restatements were added; after GREEN the C5 baseline criterion was fixed to count a run that exits non-zero. All 13 saved runs re-graded: 3 fail and 10 pass before and after.
- `qa-mutation` (Task 16): a module rename (no criterion changed); the endorsement detector (`5d04027`), which flipped run `65a27c438c5b` from fail to pass; and, in fix round 1, a score counts only from the run's own report or a structured ingest. Counted GREEN 9 of 9 before and after; four uncounted runs on an earlier text flipped from pass to fail.
- `qa-orchestrator` and `qa-reporting` (Task 18, fix round 1): two launcher criteria became hard, the pass-claim search drops retry counts, gaps must be named with their kind, `mutmut` counts as project code. Re-grade: review-granted RED 2 of 3 to 0 of 3, earlier GREEN 2 of 3 to 3 of 3; every other tally unchanged.
- `qa-reporting`'s gap matcher, reused by the F5 chain grader (Task 19, fix round 1, `4576bcf`): it now accepts the Markdown-escaped kind the kernel renders (`tc\_not\_automated`). Re-grade of every saved run it grades: F5 exit run `c6ece4592ed7` fail to pass (`report_lists_every_gap` false to true, 9 of 10 to 10 of 10 hard criteria); the six Task 18 F5 traceability runs unchanged (RED 0 of 3, GREEN 3 of 3). The F7 exit runs, which do not use it, re-graded unchanged (`cb264b3a68ca` pass, `300fdb418ea4` fail).

The one hard failure on the final orchestrator text, review-granted run `631038e08c6b`, is the pass-claim criterion on this sentence: "The sync test depends on a run counter in the pytest cache, which would explain why it passes every second run." It describes the flaky test, not the suite; it is reported as measured and marked a suspected grader false positive for the reader to judge.

Measured observations (not criteria):

- Delegation on the final orchestrator text: every subagent dispatched in the foreground, no launcher written through a shell variable, no permission denial, in all six fix-round runs. On the old text 2 of 3 review runs had dispatched in the background and recovered after a denial, so the milestone 2 failure (deviation 38) was not reproduced; the change shows in launcher form (4 variable-form calls per run before, 0 after) and denials (20 before, 0 after).
- Routing between `qa-test-strategy` and `qa-test-generation` with both installed was measured on one scenario only (the Task 18 verify-fix case).
- Routing with the whole suite installed: a plain "verify the fix" request now loads `qa-orchestrator` first (6 of 6 runs on the new text); on the old text it loaded `qa-regression-guard` alone (3 of 3). The orchestrator's RED and GREEN runs used different `qa-reporting` texts.

## Host as found

The remote engine was listed by name filter on 2026-11-11 at 18:03 UTC, while the first exit run was in progress, again at 18:19 UTC, after the last one, a third time at 19:12 UTC, after the first round of kernel legs, and a fourth time at 19:31 UTC, after the second round. The exit evaluation runs use the Mac only; the kernel legs started their own `qa-<execution id>` objects, which the kernel removes. All four times: no container, volume or network whose name starts with `qa-`. The only suite objects on the engine are the two test images of the remote test script.

Other containers were compared by creation time and uptime only; none was stopped, removed, started or inspected. At the first listing 51 non-suite containers existed, 28 running and 23 exited, and every one was created before the exit runs began. Those created before 2026-11-11 03:00 UTC that were running all showed an uptime that began around that time, which reads as a restart of the engine or its host. The project ledger records no suite execution between 2026-11-10 03:00 UTC and 2026-11-11 12:27 UTC, so the restart is not attributable to the suite from its own records; its cause is not known.

At the later listings the same 51 containers existed, with the same creation times, 28 running and 23 exited, and none had changed between running and exited.

## Spend

Read from `.superpowers/eval-ledger/m3-costs.jsonl`, last end record per run, against the A2 cap of 50 US$.

| Task | Runs | Spend (US$) |
|---|---|---|
| Task 1, harness probes | 3 | 0.3830 |
| Task 12, `qa-requirements` | 16 | 4.8722 |
| Task 13, `qa-test-strategy` | 19 | 4.2972 |
| Task 14, `qa-test-generation` | 16 | 1.9615 |
| Task 14b, harness hardening probe | 1 | 0.0864 |
| Task 15, `qa-regression-guard` | 14 | 2.0003 |
| Task 16, `qa-mutation` (one interrupted run recorded at 0.0) | 24 | 4.5087 |
| Task 18, `qa-reporting` and `qa-orchestrator` | 24 | 8.5191 |
| Task 19, exit runs | 3 | 2.2987 |
| Total | 120 | 28.9272 |

The per-task figures are the task reports' sums, rounded to four places; the total is the ledger's own sum of each run's last end record, 28.9272 US$, which leaves 21.0728 US$ under the cap. No run is open and none has an unknown cost. Task 17 had no paid runs. Task 19's three runs cost 1.3674, 0.7022 and 0.2291 US$; the F5 run's stage cap was 5 US$ and it used 1.3674.

## Installs

| Item | What | Size | Authorization |
|---|---|---|---|
| `.venv-eval` on the Mac | 19 hash-pinned packages from `requirements-mutation.txt` (mutmut 3.8.1, coverage 7.17.2, pytest 8.4.2 and their dependencies), CPython 3.12; used only as the evaluated agents' `runtime_venv` | 55M, measured at install time | A3(a) |
| Corpus producers in containers (Tasks 7 and 8) | coverage.py, Vitest with V8 coverage, Gradle with JaCoCo and PIT, StrykerJS, each in a short-lived container that was removed with `--rm` | Not measured | A3(b) |
| Kernel mutation leg (Task 17) | `requirements-dev.txt` and the mutmut corpus requirements in a venv inside the run's own container volume, removed with the run's objects | Not measured | project:AUTH-003 |
| Kernel legs (this task) | `requirements-dev.txt`, hash-pinned, in a venv inside each leg's container volume, removed with the leg's own objects | Not measured | project:AUTH-004 |

Nothing else was installed. The oasdiff image approved under A3(c) was not pulled.

## Deviations

Numbers 42 to 63 in §22.3 of the design document:

- 42 to 51: kernel scope. New commands and events for requirements, criteria, assumptions, risks, test cases, file changes and worktrees (42); new modules `records`, `fixes`, `changes`, `worktree` (43); coverage and mutation reports are measurements that never decide a run's status (44); PIT and mutmut reach the kernel through converters (45); `mutation_score_min` takes `paths` from the accepted override and `no_blocked_critical` reads a test case's criticality, a critical one whose last result is a failure giving NOT_MET, while `min_priority` has no route to set it yet (46); gap kind `tc_unproven` (47); repository `lfs.extension.*` keys neutralised, untested against a real git-lfs (48); `unpaired_expectation` and nearest-ID status judging in `qa report check` (49); `qa compare` refuses runs whose status is neither a pass nor a fail (50); `suite_executed.require_passed` stays (51).
- 52: F5's exit run is one agent invoking single skills in sequence, not a planned mode; the case lives with the orchestrator's cases only because it installs all nine skills.
- 53: the harness gives the key through an `apiKeyHelper`, generates fixtures with their history and sets write tools per case.
- 54: only the `change` and `fix` plan templates ship.
- 55: the evaluated agent's permissions were tightened in Task 14b; Tasks 12 to 14 ran with the CLI's sandbox auto-allow, so commands outside the Bash allow-list could run inside the sandbox.
- 56: skills that write their own input files run in the orchestrator's own context; only the read-and-run seats go to subagents. Through a plan, `qa-regression-guard` ran in the orchestrator's context in fix mode (Task 18) and in change mode (the F7 M1 exit run).
- 57: graders were corrected after results in Tasks 12, 14, 15, 16 and 18 (above).
- 58: text edits after GREEN without new runs: one output-sample phrase of `qa-requirements` SKILL.md and its reference examples (Task 12), the finding templates of `qa-mutation` and `qa-regression-guard` (Task 16).
- 59 and 60: the kernel mutation leg's form, scope and install (above).
- 61: `mutation_score_min` reads the score of any run whose status is a pass or a fail and that declares a mutation report, with no activity floor and no tie to a mutation tool.
- 62: F7's C2 contract-diff leg is open.
- 63: the macOS kernel legs are not executed locally (deviation 39 continues).

## Limitations and disclosures

Exit evidence:

- Each exit scenario is one run, graded once. One run shows that the chain can reach its records on that day, not how often it does.
- The exit runs gave the harness a `--timeout` (5400 s for F5, 3600 s for the F7 runs), so that a long run could not end without a cost record, which would stop every later run. No run came near its timeout.
- The kernel's own rendered report fails the kernel's own check. In the `report.md` of f5:RPT-001 the gate lines that list acceptance criteria and test cases carry a status word next to `AC-` and `TC-` IDs, which `qa report check` does not accept as citations, so it flags four `uncited_claim` lines the agent did not write. This is a kernel defect, parked for the final fix wave.
- F7's C2 leg is open (deviation 62). In the Linux kernel legs the tests that need git skip (868 per leg in the second round); they ran in the remote test script, not through the kernel. The first round's only failure was a grader test without a git guard.
- The deterministic C4 and C5 checks exercise the scan and the gate, not an agent: whether an agent records those hits as findings in one run is measured only by the Task 15 runs.

Evaluated agent and harness:

- Tasks 12 to 14 ran with the CLI's sandbox auto-allow, so a Bash command outside the allow-list could run inside the sandbox. From Task 14b on, a command outside the allow-list is denied (deviation 55); the unsandboxed-retry setting was not exercised.
- Under `allow_write`, the allow-listed launcher and `python3 -m pytest` prefixes resolve to files the agent can write, `.claude/` included, so the allow-list bounds the command form, not the code that runs.
- The permission boundary denies a compound command when any part is not allowed. Some baseline failures in Task 15 (C3, C5) came from such denials, and the F5 exit run had three denied compound commands that it re-ran as single calls.
- The read denial on the harness's helper directory is configured and was shown effective for other paths, but was not exercised against the key path itself; the evaluated model refused the probe.
- Task 16's run `f3cf0c57f675` was interrupted 2.9 s after start by the implementing agent; it is recorded at 0.0 US$ by the user's decision, with no tokens and no output, and its true cost is not known.
- Graders read the saved kernel state and file state. A file write the snapshot cannot see (an ignored path the agent adds) would be missed.

Skill texts that ship with a known inaccuracy or an unmeasured part:

- `qa-test-generation` says a `cd` lasts for one command only; a run showed it persists between Bash calls.
- `qa-regression-guard` says a compound command is denied as a whole (it is denied when any part is not allowed), and its step 4 lets a scan hit be cleared in the answer only, where the specification wants each integrity hit recorded as a finding.
- `qa-mutation` calls the gate's score "the tool's measurement", but the gate reads the score of any run that declares a mutation report (deviation 61). The PIT container form yields no score; the StrykerJS container form is untested.
- Of the orchestrator's in-context delegation rows, `qa-requirements`, `qa-test-strategy` and `qa-mutation` have no run through a plan (deviation 56). The F5 exit run used the first two in its main context, but it recorded no plan and dispatched no subagent, so it does not exercise those rows.
- Post-GREEN text edits without runs (deviation 58).
- `qa-test-strategy`'s hand-off to `qa-test-generation` by name held in 2 of 3 runs.

Kernel:

- The redaction rule `secret_assignment`, applied when a declared report is stored, can cut through a JSON string and make the stored report invalid; this hits mutation reports of projects whose code assigns to secret-like names, not only the kernel's own `report/check`. Parked for the final fix wave.
- `min_priority` has no route to set it (deviation 46). A coverage XML file with a DOCTYPE must be ASCII-compatible, and a literal `&` in its comments or CDATA is refused.
- The `lfs.extension.*` neutralisation is untested against a real git-lfs (deviation 48).
- The kernel mutation tool's host-side config route needs an untracked, non-ignored `setup.cfg` at the repository root; it does not warn about it, a stale file rides along into later runs, and `git add -A` would commit it. The route has not been executed on the engine.

Spend and billing:

- The evaluation key bills the user's Console credit. On 2026-11-10 the user reported a balance 16.13 US$ lower, against a harness ledger total of 16.0860 US$ at that time, a difference of 0.044 US$; no later reconciliation was made.

## Approvals

- A2, A3 and A4 (the user, 2026-11-10, answering the plan questions): the plan with a 50 US$ evaluation cap ("Aprobar con 50 US$ (Recomendado)"); installs "mutmut y coverage en Mac", "Herramientas JS/Java en contenedor", "oasdiff (opcional)"; "Contenedores remotos".
- project:AUTH-003, `install-dependencies`, environment `local`: the user's answer of 2026-11-11 "Sí, autorizo por 6 horas (Recomendado)", for the Task 17 mutation leg only.
- project:AUTH-004, `install-dependencies`, environment `local`: the user's answer of 2026-11-11 "Sí, 6 horas (Recomendado)", for this task's kernel legs only.
- Trust in each exit evaluation run: recorded, or not, by the agent from the user's words in that run's prompt.
- The deterministic F7 workspaces record no trust and no approval.

## Checks

- The `qa report check` command in the introduction, with its five `--ledger` options, returns `ok: true` with no problems. Without them it reports 60 `unknown_id` and 13 `uncited_claim` problems, for IDs held only in the extra ledgers and the lines whose only IDs are those.
- `python3 -I tools/check_repo.py`: 0 problems. `tools/vendor_kernel.py --check`, `tools/validate_skills.py` and `tools/check_catalog.py`: 9 skills, 0 differences and 0 problems.
- The grader commit `7657b8f` changed evaluation code, the two exit cases and their tests only; the static skill tests ran on Linux with Python 3.9 and 3.15 through the remote test script before the exit runs, and both exited 0.
