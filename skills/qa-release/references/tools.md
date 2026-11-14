# Tools

This skill installs nothing and downloads nothing. Its own scripts use only Python's standard library. Every external tool
(actionlint, zizmor) runs only through the kernel, in a container from an image pinned by digest, with no network. The
kernel never pulls an image. An engine that does not answer, an image that is not on the machine, or a guard refusal makes
the run `blocked`: that record is the result. Never run a tool another way (a local install, `brew`, `pip install`, `go
install`, `npx`, a binary found on the PATH, a hosted scanner, a GitHub App), and never look for one on this machine
(`which`, `--version`).

Every kernel call is written out in full, from the project root:

`python3 .claude/skills/qa-release/scripts/qa.py --role auditor --skill qa-release <command>`

## Kernel commands used

| Command | Use |
|---|---|
| `init`, `config show` | The workspace: the declared environments (a `ci` environment and its standing authorization), the grants, the trust |
| `run --container IMAGE@DIGEST [--activity static-analysis] --category pipeline --timeout N [--input .qa/work/release/FILE] [--report sarif:{evidence}/FILE] --shell -- 'SHELL'` | One tool run in a container. The project is copied to `/work` (`.qa/` stays out) and the command starts there; an `--input` file is at `/work/.qa-input/release/<name>`; `{evidence}` is `/qa-evidence` inside. Without a `--report`, the run's stdout is what is kept (`.qa/evidence/<EXE>/stdout.log`). A container run on `local` needs no trust and no grant. Exit codes: 0 passed, 1 failed, 3 blocked, 4 inconclusive, 5 not_run |
| `ingest sarif .qa/work/release/<file>.sarif --from <EXE> --category pipeline` | Attach a converted SARIF report to the run that produced it |
| `finding add --json .qa/work/release/<name>.json`, `finding update FND --json FILE`, `finding state FND duplicate --reason R --duplicate-of KEEP` | One finding, a change to one, one record of a problem marked as a duplicate of another (`references/triage.md`) |
| `gate evaluate --profile release` | The release gate's verdict and each criterion's outcome (`references/release-criteria.md`) |
| `gate policy propose CRITERION --override JSON --rationale R` | A proposed change of one criterion, with its reason; it changes nothing until the user accepts it |
| `ci generate --platform github-actions --environment ENV --execution EXE [--execution EXE ...]` | The re-execution job, written as a patch under `.qa/work/patches/` (`references/ci-reexecution.md`) |
| `state show [executions\|findings\|authorizations]`, `ledger verify` | What was recorded |

Never call `authorize`, `config set`, `config accept` or `gate policy accept`: a grant, a trust change, a configuration change
and an accepted override are the user's own decisions, made with their own words, never by this skill.

The skill's scripts read files and write only under `.qa/work/release/`; they run directly, from the project root:

- `python3 .claude/skills/qa-release/scripts/workflow_scan.py --root . --out .qa/work/release/workflow-scan.sarif`: the
  GitHub workflows (below). `--copy-to-work` copies the script to `.qa/work/release/workflow_scan.py` for its container leg.
- `python3 .claude/skills/qa-release/scripts/release_check.py --root . --out .qa/work/release/release-check.sarif`: rollback
  documentation, base images, other CI systems (below). `--copy-to-work` copies it to `.qa/work/release/release_check.py`.
- `python3 .claude/skills/qa-release/scripts/actionlint_to_sarif.py --input .qa/evidence/<EXE>/stdout.log --out
  .qa/work/release/actionlint.sarif`: turns a stored actionlint JSON report into SARIF of category `pipeline`.

## The images

| Tool | Version | Image (use exactly this) |
|---|---|---|
| actionlint | 1.7.12 | `rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667` |
| zizmor | 1.32.0 | `ghcr.io/zizmorcore/zizmor@sha256:3014cc8f68f663935f69b3c3e0e41f5ce102b268d97db13db0f62208747f860d` |
| Python, for the skill's own checks | 3 (slim) | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` |

An image not in `.claude/skills/qa-release/scripts/_kernel/policies/container-images.json` is not used. No image of a
deploy tool, a registry client or a GitHub client is used, and none is added: this skill never deploys or publishes.

## The workflow scan

1. Function: `scripts/workflow_scan.py` reads every workflow directly in `.github/workflows/` with its own reader for the
   block-style YAML workflows are written in, and writes SARIF 2.1.0 of category `pipeline` with CWE references. Rules:
   `unpinned-action` (CWE-829), `pr-target-head-checkout` (CWE-829), `expression-in-run` (CWE-94), `tests-allowed-to-fail`,
   `secret-echoed` (CWE-532), `excessive-permissions` (CWE-250).
2. Use cases: the first step of every pipeline review: what the workflows run, with which references, triggers, secrets and
   token permissions, each with the file and line a person can open.
3. Compatibility: GitHub Actions workflows only. The reader takes block mappings and sequences, plain and quoted one-line
   values, block values (`|`, `>`), comments and flat one-line flow collections. A line it cannot read exactly (an anchor,
   an alias, a merge key, a tab, a value continued on another line, a nested flow collection ...) is listed `unreadable` with
   its line number and reason, and nothing under it is read: name it, never guess what it holds. A composite action's own
   file, a called workflow in another repository and the repository's settings are not read.
4. Dependencies: Python 3.9 or newer, standard library only. In the container leg, the Python image above and the copy that
   `--copy-to-work` writes.
5. Alternatives: zizmor and actionlint (below), which read the same files with full YAML parsers; a person's reading.
6. License, costs, restrictions: part of this skill (Apache-2.0); no network. Files over 1 MiB, links, files that are not
   regular or not UTF-8 are listed `unreadable`, and nothing is reported from them. A line over 4096 characters is listed
   `unreadable` with its number before any rule reads it.
7. Reason selected: it runs everywhere with no install and no engine, and cites the file and line.

```qa-leg workflow-scan grant=none report=sarif
python3 .claude/skills/qa-release/scripts/qa.py --role auditor --skill qa-release run --container python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --category pipeline --timeout 300 --input .qa/work/release/workflow_scan.py --report 'sarif:{evidence}/workflow-scan.sarif' --shell -- 'cd /work && python3 -I -B /work/.qa-input/release/workflow_scan.py --root . --out /qa-evidence/workflow-scan.sarif'
```

## The release check

1. Function: `scripts/release_check.py` reads the documentation, the Dockerfiles and Containerfiles, and other CI systems'
   files, and writes SARIF 2.1.0 of category `pipeline`. Rules: `rollback-documentation` (deploy steps with no documented
   way back), `base-image-digest` (CWE-829, a `FROM` not pinned by `@sha256`), and the generic checks, each labelled
   `generic`: `generic-masked-gate`, `generic-unpinned-image` (CWE-829), `generic-secret-echoed` (CWE-532). It also lists a
   stage inventory (stages and jobs with their lines), which is not a result.
2. Use cases: what a release depends on beyond the workflows: whether a failed deploy has a way back, whether the image a
   release builds from can change under it, and the GitLab, Jenkins, Azure Pipelines or CircleCI files of a project that
   uses them.
3. Compatibility: Markdown, reStructuredText, AsciiDoc and text documentation; Dockerfile syntax; `.gitlab-ci.yml`,
   `Jenkinsfile`, `azure-pipelines.yml` and `.circleci/config.yml` read as text, so every generic result is approximate: a
   lead for a person, never that system's own verdict. An image set by a build argument or a variable goes to `notes`, and
   so does each `build`, `dist`, `target`, `vendor` or `coverage` directory it skips: a Dockerfile, CI file or document
   kept there is not checked, so read it yourself if the project keeps one there.
4. Dependencies: Python 3.9 or newer, standard library only. In the container leg, the Python image above and the copy that
   `--copy-to-work` writes.
5. Alternatives: hadolint for Dockerfiles; each CI system's own linter (GitLab's CI lint, the Jenkins pipeline linter);
   a person's reading of the runbook.
6. License, costs, restrictions: part of this skill (Apache-2.0); no network. Files over 1 MiB, links and files that are
   not regular or not UTF-8 are listed `unreadable`, as is a line over 4096 characters (with its number) and a directory
   more than 64 levels below the root.
7. Reason selected: the release process lives in documents and files no workflow linter reads.

```qa-leg release-check grant=none report=sarif
python3 .claude/skills/qa-release/scripts/qa.py --role auditor --skill qa-release run --container python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 --category pipeline --timeout 300 --input .qa/work/release/release_check.py --report 'sarif:{evidence}/release-check.sarif' --shell -- 'cd /work && python3 -I -B /work/.qa-input/release/release_check.py --root . --out /qa-evidence/release-check.sarif'
```

The kernel reads each leg's SARIF: a run with results is `failed`, and each result opens a provisional finding of the run's
category, `pipeline`, unless a finding with the same rule, file and category is already recorded. So record the host
check's findings with `finding add` first, each with `refs.rule` the rule id, `component.path` and `refs.location` the file,
and no `key_attributes` (`references/triage.md`): the leg's result for the same rule in the same file is then a new
sighting of that finding, not a second record.

## actionlint: check workflow syntax, expressions and scripts

1. Function: a static checker for GitHub Actions workflows: syntax and keys, `${{ }}` expression types, references to
   undefined steps, jobs or inputs, runner labels, and untrusted inputs in `run` scripts (its `expression` check flags an
   event field another person controls).
2. Use cases: every workflow under review; backing an injection finding with the tool's own rule and line.
3. Compatibility: GitHub Actions workflow files. Its JSON report (`-format '{{json .}}'`) lists errors with `filepath`,
   `line`, `column`, `kind` and `message`; it has no SARIF reporter, so `scripts/actionlint_to_sarif.py` converts it.
4. Dependencies: the image above; no service, no network, no grant (`--activity static-analysis`).
5. Alternatives: zizmor (below); the workflow scan; the editor's workflow schema.
6. License, costs, restrictions: MIT, free; used as an unmodified binary in its own image. It sends nothing; the container
   has no network.
7. Reason selected: the reference linter for workflow correctness, with stable check names and lines.

Exit 1 means errors were found, so a run with errors is `failed`; the errors are in the run's `stdout.log`.

```qa-leg actionlint grant=none report=none
python3 .claude/skills/qa-release/scripts/qa.py --role auditor --skill qa-release run --container rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 --activity static-analysis --category pipeline --timeout 300 --shell -- 'actionlint -no-color -format "{{json .}}" .github/workflows/*.y*ml'
```

Then convert the stored JSON and attach it to that run, `<EXE>` being the run's execution id. Only for a run that is
`failed` or `passed`: a `blocked` run stored nothing.

```qa-leg actionlint-sarif grant=none report=sarif
python3 .claude/skills/qa-release/scripts/actionlint_to_sarif.py --input .qa/evidence/<EXE>/stdout.log --out .qa/work/release/actionlint.sarif
```

```qa-leg actionlint-ingest grant=none report=sarif
python3 .claude/skills/qa-release/scripts/qa.py --role auditor --skill qa-release ingest sarif .qa/work/release/actionlint.sarif --from <EXE> --category pipeline
```

## zizmor: find security weaknesses in workflows

1. Function: a static security analyzer for GitHub Actions: template injection, dangerous triggers
   (`pull_request_target`, `workflow_run`), checkouts that persist credentials, references not pinned by commit, excessive
   permissions, cache poisoning, secrets passed where they should not go.
2. Use cases: every workflow under review; the security classes of a pipeline finding, with the tool's rule and line.
3. Compatibility: GitHub Actions workflows and action definitions. `--offline` keeps it from asking GitHub anything (no
   tag lookup, no advisory lookup), so the audits that need the network do not run; SARIF on stdout with `--format sarif`.
4. Dependencies: the image above; no service, no network, no grant (`--activity static-analysis`). Without `--offline` the
   guard asks for a `data-egress` grant, which this skill never records.
5. Alternatives: actionlint (above); the workflow scan; GitHub's code scanning for Actions.
6. License, costs, restrictions: MIT, free; used as an unmodified binary in its own image, offline. It sends nothing.
7. Reason selected: the reference security analyzer for workflows, with SARIF output and stable rule ids.

```qa-leg zizmor grant=none report=sarif
python3 .claude/skills/qa-release/scripts/qa.py --role auditor --skill qa-release run --container ghcr.io/zizmorcore/zizmor@sha256:3014cc8f68f663935f69b3c3e0e41f5ce102b268d97db13db0f62208747f860d --activity static-analysis --category pipeline --timeout 300 --report 'sarif:{evidence}/zizmor.sarif' --shell -- 'zizmor --offline --no-progress --format sarif . > {evidence}/zizmor.sarif'
```

zizmor exits 0 with `--format sarif` even when it reports results; the kernel reads the SARIF, so a run with results is
`failed`. A linter's result for a problem you already recorded (its rule id differs from the scan's) opens a second record
of it: link the two as `references/triage.md` says. Quote rule ids and lines from the stored report, never from memory.

## When a tool cannot run

| The run's record | What it means | What you say |
|---|---|---|
| `blocked`, `dependency_unavailable`, limitation `container_engine_unavailable` | No container engine answered | "actionlint did not run: no container engine on this machine (EXE-…)". No result, no estimate of what it would find |
| `blocked`, `container_image_missing` | The pinned image is not on the machine; the kernel never pulls | The image the user would need, by digest |
| `blocked`, `policy_denied` | The guard refused the command | Read the reason; never rewrite the command to slip past it |
| `blocked`, `not_authorized` | A grant or the trust the run needs is missing | Never record it; say what the run needs |
| `failed` | The tool ran and reported problems | Its results, by rule and line, with the EXE; each a sighting of your findings or a new provisional finding |
| `passed` | The tool ran and reported nothing | "zizmor ran (EXE-…) and reported nothing offline"; never "secure" |

A blocked run's step is not done. Your reading and the skill's own checks still stand, under their own names, as
`suspected` findings; never relabel them as a linter's result.
