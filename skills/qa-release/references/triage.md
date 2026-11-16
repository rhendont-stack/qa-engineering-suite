# Triage

One finding per problem, each written to `.qa/work/release/<name>.json` with Write and recorded with
`finding add --json .qa/work/release/<name>.json`. A problem your reading or the skill's checks found is recorded even when
no linter ran: that is what `suspected` is for. Category `pipeline`, whose anchors are rated in the `FUNC` column of
`<skill-dir>/scripts/_kernel/policies/severity-rubric.json`; the kernel refuses an anchor of another column.

## How sure: `verification_status`

| Status | When |
|---|---|
| `suspected` | Read in the files, or listed by the workflow scan, the release check or a linter. The status of every finding of a review: nothing in a pipeline review runs the pipeline |
| `confirmed` | Only when a recorded run of the pipeline itself, attached with `qa ingest`, shows the behaviour, with its EXE or bundle id in `evidence`. A linter's result is a reading of the file, not the behaviour: still `suspected` |
| `false_positive` | A file shows the result does not apply (the step that looks unpinned uses a local action); the finding names the file and line that show why. Never because someone says it is fine |

## The finding template

```json
{
  "title": ".github/workflows/release.yml:42: the deploy step prints the registry token to the job log (CWE-532)",
  "description": "The publish job sets REGISTRY_TOKEN from secrets.REGISTRY_TOKEN in env (.github/workflows/release.yml:36), and the run script at .github/workflows/release.yml:42 echoes it, so the token is written to the job log of every run. Read from the workflow and listed by the workflow scan (secret-echoed); the workflow was not run.",
  "category": "pipeline",
  "severity_rationale": {"anchor": "FUNC-M1", "modifiers": []},
  "component": {"path": ".github/workflows/release.yml", "criticality": "normal"},
  "verification_status": "suspected",
  "evidence": [".github/workflows/release.yml:42", ".qa/work/release/workflow-scan.sarif"],
  "reproduction_steps": ["Open .github/workflows/release.yml at line 36: env REGISTRY_TOKEN from secrets", "Read line 42: echo of $REGISTRY_TOKEN in the run script"],
  "impact": "Anyone who can read the job's logs can read the token, and GitHub's masking does not cover a transformed value.",
  "probable_cause": ".github/workflows/release.yml:42 echoes a variable set from a secret",
  "recommendation": "Stop printing the token; pass it to the program that needs it through env. The change to the workflow needs a human review before it is merged; nothing was changed here.",
  "refs": {"rule": "secret-echoed", "location": ".github/workflows/release.yml", "cwe": ["CWE-532"]}
}
```

- `title` and `probable_cause` start with `path:line`, the path from the project root, the line as the check's `at` gives
  it or as Read numbers it, never a line counted by eye. A problem with two places (a trigger and the checkout it makes
  dangerous) cites both in the description, the result's line in the title.
- `refs.rule` is the check's rule id, or `static-review` for a problem you read that no rule lists. `refs.location` and
  `component.path` are the file. `refs.cwe` is the rule's CWE list, copied from the result (none for a rule without one).
  Leave out `key_attributes`: a leg's result for the same rule in the same file is then a new sighting of this finding,
  not a second one.
- Two results of one rule in one file (two actions referenced by tag) are one finding: list each with its line in the
  description.
- `criticality` is `critical` for a workflow or job that deploys or publishes, `normal` otherwise.
- The recommendation says what to change and that the change needs a human review; you change nothing.

## Rules and their anchors

| Rule | Anchor | Why this anchor | What a person checks beyond the rule |
|---|---|---|---|
| `pr-target-head-checkout` | `FUNC-H2` | Code from a pull request runs with the repository's token and secrets: the pipeline's integrity is at risk under a realistic condition, a pull request from a stranger | whether a later step runs what was checked out (a build, a script, an install) |
| `expression-in-run` | `FUNC-H2` | A title, a branch name or a comment becomes shell code in the job | which event fields reach the script, and which secrets the job holds |
| `unpinned-action` | `FUNC-M1` | A tag the action's owner moves changes what the job runs | whether the action is the project's own organization's |
| `tests-allowed-to-fail` | `FUNC-M1` | A failing test run does not fail the job, so the gate it is meant to be is gone | whether a later step reads the test outcome |
| `secret-echoed` | `FUNC-M1` | A credential in the job log | who can read the logs (a public repository's logs are public) |
| `excessive-permissions` | `FUNC-M2` | The token can do more than the jobs need; a workaround exists (per-job scopes) | which job uses which scope |
| `rollback-documentation` | `FUNC-M2` | A failed deploy has no documented way back; a person can still improvise one | whether a runbook outside the repository holds it (ask; never assume) |
| `base-image-digest` | `FUNC-L1` | The base image can change under the same build | whether the image is rebuilt and scanned on a schedule |
| `generic-masked-gate` | `FUNC-M1` | As `tests-allowed-to-fail`, in another CI system | the line itself: a generic result is a lead |
| `generic-unpinned-image` | `FUNC-L1` | As `base-image-digest`, for a CI job's image | the line itself |
| `generic-secret-echoed` | `FUNC-M1` | As `secret-echoed`, by the variable's name only | whether the variable holds a secret |
| `static-review`: a deploy that starts the new version before its migration, or a destructive migration in the same release | `FUNC-H2` | A rollout or a rollback can meet a schema it cannot use | the order of the steps, the migration's content |
| `static-review`: a deploy with no condition, no test dependency or no concurrency group | `FUNC-M2` | An untested or overlapping deploy | the trigger, `needs:`, `concurrency:` |
| `static-review`: an artifact rebuilt per environment, or consumed unchecked by a privileged job | `FUNC-M2` | The deployed build is not the tested one | where the artifact is built and read |

A modifier applies only when the file shows it: `dev_only` for a workflow that only a developer's manual dispatch runs
and that holds no secret, `unreachable` for a job no trigger can start.

## Negative evidence: what is not a finding

- `uses: owner/action@<40 hex digits>` is pinned, with or without a `# v4` comment after it. Never report it as unpinned,
  mutable or by tag.
- `permissions:` with `contents: read` (at the top or on the job) is least privilege. Never report it as excessive,
  broad, write or missing. A workflow that declares it has no permissions finding, unless a job widens it.
- `pull_request` (not `pull_request_target`) runs a fork's code with a read-only token and no secrets: not the privileged
  trigger class.
- `${{ github.sha }}`, `${{ github.run_id }}`, `${{ github.event.pull_request.number }}` are not text a person controls.
- `FROM scratch` and `FROM <earlier stage>` are not base images to pin.
- The checks' `notes` (an image set by a build argument) are things to mention, not findings, unless your reading shows
  a problem.

## Two records of one problem

A linter's rule ids differ from the scan's (zizmor's `zizmor/unpinned-uses`, `zizmor/dangerous-triggers` and
`zizmor/template-injection`, actionlint's `expression`), so a linter result opens a provisional finding of its own beside
yours for the same problem. zizmor's cites a privileged trigger at the trigger's line, where the scan cites the checkout and
names the trigger as its related location: one problem. Keep yours and link the other in two calls:

1. `finding update <KEEP> --json .qa/work/release/<keep>-update.json`: add to KEEP's `evidence` what the other adds (its
   EXE). The link itself moves nothing.
2. `finding state <OTHER> duplicate --reason "<why they are one problem>" --duplicate-of <KEEP>`.

A linter result no finding of yours covers is a new lead: read the line it cites, then triage it out of its provisional
anchor with `finding update <FND> --json <file>` (a rubric anchor from the table above and the CWE), or mark it
`false_positive` with the file and line that show why.
