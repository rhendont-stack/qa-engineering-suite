# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project will adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The `qa` kernel, on Python 3.9 or later with the standard library only: workspace, IDs, canonical JSON, hash-chained ledger, derived state, schema validation, process runner, status rules, command guard, authorization, configuration changes and drift detection, secret redaction, and read-only git information.
- Ingestion of JUnit, CTRF and SARIF reports.
- Findings, release gates, Markdown and JSON reports, and checking of the claims a report makes.
- JSON Schemas, gate profiles and command-guard policies.
- The kernel's test suite, repository checks and a GitHub Actions workflow.
- Spine skills: `qa-orchestrator` with the `change` plan template, `qa-test-execution`, `qa-codebase-intelligence` and `qa-reporting`, three Claude Code subagents (`qa-auditor`, `qa-verifier`, `qa-evaluator`), and a skill catalog.
- Container runs: `qa run --container IMAGE@DIGEST` with no network and no bind mount, an optional `--install` step under its own authorization, and a configured container engine.
- `qa plan`, `qa compare`, `qa catalog`, evidence bundle export (`qa report bundle`) and import, `qa finding dedupe`, an executive one-page report, and SARIF as a report format (`qa report render --format sarif`).
- A skill evaluation harness, `tools/run_skill_eval.py`, with a cost ledger and a budget cap.
- `qa report check --ledger [ALIAS=]PATH` (repeatable): a report may cite evidence held in other ledgers, and cite an aliased ledger's IDs as `ALIAS:GATE-001`. Each extra ledger is opened read-only and must verify like `qa ledger verify`. A bare numbered ID (`GATE-`, `RPT-`, `AUTH-`, `PLAN-`, `FND-`) that more than one ledger holds is ambiguous; an `EXE-` or `RES-` ID the ledgers record differently is a conflict. A verdict word is judged against every cited gate and the gate of each cited report, and `expected: TOKEN` marks a token as an expectation rather than a claim.
- `tools/check_repo.py` flags IP and email addresses in `tests/` and `tools/`, and any string listed in an optional, git-ignored `.private-terms` file.
- The [milestone 2 report](docs/reports/milestone-2.md). It records the exit as measured: the Linux kernel legs first failed on tests that need `git` and passed after a test-only guard, the F9 review through the orchestrator needed a scenario correction and then ended `NOT_READY`, which is correct for the whole fixture because one of its runners records a genuine failure (only scenario F9-01, the absent tool, alone gives `INSUFFICIENT_EVIDENCE`, shown by a deterministic run), and the macOS legs were not run locally.

### Security

- The kernel's git reads no longer run a filter or diff program that a repository configures for itself (its `.git/config`, an included file, `config.worktree` or a submodule); a user-level driver such as git-lfs still applies. The diff hash is computed as a stream.
- `tools/run_skill_eval.py` deletes each run's working copy after saving its `.qa/` state, and redacts hex and base64 forms of the API key as well as the key itself.
- An `ssh` container engine runs with `LogLevel=ERROR`, so login banners stay out of the logs.
