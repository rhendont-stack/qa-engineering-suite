# Applicability

How to choose the mode, which steps apply, and what to hold back. Read at step 1 and again at step 5 of the procedure.

## Modes

| Mode | Typical request | Template in this release | Gate profile | Minimum independence |
|---|---|---|---|---|
| Single skill | "Run mutation testing on `src/cart`", "map this codebase", "run the tests" | none: hand the request to the named skill | none, no verdict | none |
| Targeted review | "Review this PR", "review this change", "the agent says feature X is done" | `assets/plans/change.json` | `change` | I1, I2 recommended |
| Fix verification | "Verify the fix for FND-012", "verify the fix the developer pushed" | `assets/plans/fix.json` | `fix` | I1 |
| Full audit | "Audit this application" | not in this release | `audit` | I2 |
| Pre-production validation | "Is release 2.3.0 ready for production?" | not in this release | `release` | I2, I3 recommended |

A mode with no template cannot be planned. Say that it is not in this release, offer the targeted review of the same target or the one skill that fits, and wait for the user's choice. Do not assemble a plan by hand from another mode's steps.

When the request could be a single skill or a review, prefer the single skill if the user named an activity ("run", "map", "report") and the review if the user named a purpose ("review", "check what the agent did", "is this safe to merge"). A request to verify, confirm or check a fix someone pushed is a fix verification, even when no finding ID is named: the finding the fix claims to close is the one `state show findings` lists open, and if several are open, the one the request describes.

## The targeted review plan

| Step | Skill | Runs | Needs | Done when |
|---|---|---|---|---|
| S01 | `qa-codebase-intelligence` | maps the code, scoped to the area under review | static analysis (allowed by default) | a valid map pinned to the plan's commit exists under `.qa/map/` |
| S02 | `qa-regression-guard` | checks that the claimed change did not break what passed before | running tests on this machine | an execution and its results by that skill at the plan's commit |
| S03 | `qa-test-execution` | runs the project's existing tests and records each run | running tests on this machine | an execution and its results by that skill at the plan's commit |
| S04 | `qa-reporting` | evaluates the gate and renders the checked reports | none | a gate evaluation of profile `change` and a rendered report at the plan's commit |

## The fix verification plan

The plan's commit is the fix (`HEAD`); the base is the commit before it.

| Step | Skill | Runs | Needs | Done when |
|---|---|---|---|---|
| S01 (G) | `qa-regression-guard` | runs the base and the fix in suite worktrees, records the fix (`FIX-`) for the open finding, and the kernel's FAIL_TO_PASS and PASS_TO_PASS revalidation (`RVL-`) | running tests on this machine; a base commit | an execution and its results by that skill at the fix commit, and a revalidation of a fix whose commit is the plan's, whatever its outcome |
| S02 (F) | `qa-test-execution` | runs the project's whole suite at the fix and records it | running tests on this machine | an execution and its results by that skill at the plan's commit |
| S03 (P) | `qa-reporting` | evaluates the `fix` gate and renders the checked reports | none | a gate evaluation of profile `fix` and a rendered report at the plan's commit |

A revalidation that came out `failed` or `inconclusive` still settles S01: the step is done because the record exists, and the gate and the answer say what the outcome was. Never describe the fix as verified unless the `RVL-` outcome is `passed`.

## Using a template

A template is used whole. Whether a skill is installed is decided by the kernel, not by you: `plan status` marks a step whose skill is missing as `blocked` and prints the remedy. A step whose skill is missing stays `blocked`; the rule below for a base commit applies only to a `qa-regression-guard` step whose skill is installed.

## What to hold

Hold a step only with the launcher call `python3 .claude/skills/qa-orchestrator/scripts/qa.py --role coordinator --skill qa-orchestrator plan update <PLAN> <STEP> --status not_run --reason "<reason>"`, and only for one of these reasons:

- The user descoped it in their own words. Quote them in the reason.
- It cannot apply to this target. Example: S01 of a targeted review for a change that touches only documentation. Say what you saw that makes it inapplicable.
- A `qa-regression-guard` step (installed, and `ready` or `pending`) with no base commit: `git rev-parse HEAD~1` fails. Reason: `no base commit to compare`.
- The time or cost limit the user gave is used up: reason `out_of_budget`, lowest-priority steps first.

Never hold a step because it is hard, slow, or because its skill is missing (that is `blocked`, by itself).

## Scope

- "Scoped" means the paths the change touches. Pass them to the mapping step. With a base branch, the paths are what `git diff --name-only <base>...HEAD` lists.
- With a single commit, or with no base to compare against, the scope is the whole tree at `HEAD`. State that as an assumption in the first answer. It is not a reason to ask the user anything.
- A dirty tree is noted in the summary: results are tied to the commit and to the working-tree diff the kernel recorded.

## Skills outside a template

The full catalog is larger than what a template names. `catalog list` shows what is installed. A skill for an area the change touches (requirements, API, database, security, performance, front end, mobile, mutation, test generation, release) is not added as a step of these templates. Name the area in the answer's "Not available here" line when it is not installed, so the user knows it was not covered. A plan step may name only a skill the template names.

## Conditional skills

A step for another skill is added only if its trigger holds: tickets or stories exist (requirements), the change touches HTTP routes (API), schemas or queries (database), authentication or input handling (security), or critical code (mutation). Test generation is added only when the user authorized writing tests. Write the extended template under `.qa/plan/` and create the plan from it; never edit a file under `assets/`. Which subagent runs each of these skills: `references/delegation.md`.
