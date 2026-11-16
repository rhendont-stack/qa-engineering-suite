# The CI re-execution job

`qa ci generate` writes a GitHub Actions workflow that re-runs, on a clean runner, the runs this workspace recorded on this
machine, and uploads their evidence as one bundle. It writes the workflow as a patch under `.qa/work/patches/`, never into
the project: putting it in the project is the user's step, after a human review. This skill never writes under
`.github/`, never applies the patch, never commits, pushes or opens a pull request, and never offers to.

## Before generating

1. `config show`: the environments. The job needs one of class `ci` (its name, for `--environment`) and a standing
   authorization that covers the runs' activity on it. If there is none, stop: declaring an environment and authorizing it
   are the user's decisions, made with `config set` in their own words. Say what is missing; never record it.
2. `state show executions`: the runs the job can re-run. Each is a host run (not `--container`) whose status is `passed`
   or `failed`, recorded on a clean tree at a commit. A `blocked` or `not_run` run never executed and cannot be re-run.
   One workflow checks out one commit: give the runs of one commit.
3. The configuration and the launcher at that commit: the job pins itself to the accepted `.qa/config.json` as the
   commit holds it, and runs the launcher the project tracks there, a project copy of this skill under `.claude/skills/qa-release/`
   (its `scripts/qa.py` and `scripts/_kernel/`). A runner sees only the repository, never the directory a plugin or the
   user's own skills were installed in. When `<skill-dir>` is that project copy, `ci generate` takes its launcher; when it is
   not, add `--launcher .claude/skills/qa-release/scripts/qa.py` to name the copy. Both must be committed; the kernel refuses
   otherwise (`ci_launcher_untracked`, `ci_config_untracked`) and says which.

## Generating

`python3 <skill-dir>/scripts/qa.py --role auditor --skill qa-release ci generate --platform github-actions --environment <ci environment> --execution <EXE> [--execution <EXE> ...]`

The reply names the `patch` (`.qa/work/patches/qa-ci-<12 hex>.patch`), its `sha256`, the `workflow_path` it creates
(`.github/workflows/qa-reexecution.yml`, or `--name` for another name), the `target_commit`, the `executions` it re-runs
and the `skipped` ones with the reason. Read the patch with Read before you describe it.

| Refusal | What it means | What you say |
|---|---|---|
| `ci_environment_invalid` | No environment of class `ci` by that name, or no standing authorization covers the runs | The environment or the authorization the user would declare; never declare it |
| `ci_no_executions` | None of the runs can be re-run (each `skipped` entry says why) | The reasons, run by run |
| `ci_mixed_commits` | The runs are at different commits | Generate one workflow per commit |
| `ci_launcher_untracked`, `ci_config_untracked` | The launcher or the accepted configuration is not committed at the target commit | That the user commits it first; you commit nothing |
| `ci_workflow_exists`, `ci_patch_exists` | The workflow is already in the project, or this patch was already written | The existing file or patch; nothing is overwritten |
| `ci_argv_unrepresentable` | A run's command holds `${{` or a line break, which a workflow cannot carry safely | The run, by EXE |
| `ci_time_budget_exceeded` | The runs' timeouts add up to more than a hosted runner allows | Fewer runs per workflow |
| `config_drift` | `.qa/config.json` changed since it was accepted | That the user reviews and accepts it; never accept it |

## What the job does

- It runs on `workflow_dispatch`, and on a push that changes its own file.
- It declares `permissions: {}` for the workflow and `contents: read` for its one job, and checks out the target commit
  with `persist-credentials: false`, using actions pinned by commit.
- `qa ci begin --config-hash <the accepted hash>` pins the runner's workspace to the configuration accepted when the job
  was generated.
- It re-runs each recorded command with its own timeout and report, bundles the evidence, and uploads it as the artifact
  `qa-evidence-bundle`, kept 7 days.

## What the job never does

- It computes no verdict: a CI-pinned workspace refuses `gate evaluate`. The verdict comes from where the bundle is
  imported: download the artifact, then `qa ingest bundle <file>` in a workspace on a person's machine, then
  `gate evaluate --profile release` there.
- It records no grant and changes no configuration: a CI-pinned workspace refuses both.
- It deploys, tags, pushes and publishes nothing, and it needs no secret.

## Applying the patch

The user applies it, never this skill:

1. A person reviews the patch: what it re-runs, at which commit, with which permissions.
2. The user applies it from the project root with their version control tool's patch-apply command, then commits the new
   workflow and merges it through the project's own review, like any change to `.github/`.
3. The job runs on that push, or by hand from the repository's Actions page.

The answer names the patch by its path under `.qa/work/patches/`, the workflow it creates, the runs it re-runs, these
three steps, and that the job computes no verdict: the bundle is imported with `qa ingest bundle` where the gate is
evaluated.
