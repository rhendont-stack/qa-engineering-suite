# Contributing

The project is pre-release: the kernel and all 16 skills exist, and no version has been released. Please read the [code of conduct](CODE_OF_CONDUCT.md) first, and report security problems as described in the [security policy](SECURITY.md).

## Development setup

You need Python 3.9 or later. The kernel uses only the standard library; the development dependencies are for its test suite.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
```

Run the tests from the repository root:

```bash
.venv/bin/python -m pytest -q
```

Run the repository checks:

```bash
.venv/bin/python -I tools/check_repo.py
```

Tests that need a tool your machine lacks, such as git, skip and give the reason. Some tests also depend on the file system or the user they run as.

## Rules for the kernel

- Standard library only, and Python 3.9 syntax. Every module starts with `from __future__ import annotations`, except `shared/launcher/qa.py`.
- Errors are `QaError(code, message, **details)`. Tests assert on `.code`, not on message text.
- Every command prints one JSON object on stdout.
- No empty or placeholder files.
- Documentation, messages and identifiers are in English.

## Writing or changing a skill

A skill is `skills/<name>/`: `SKILL.md`, its `references/` and `scripts/`, a vendored kernel under `scripts/_kernel/` with the launcher `scripts/qa.py`, and its evaluations under `evals/`.

- `SKILL.md` follows the [Agent Skills specification](https://agentskills.io): block-style frontmatter with only the specification's fields, `name` equal to the directory name, a `description` of one line and at most 300 characters, every `metadata` value a string, and `metadata.suite-version` equal to the other skills'. The body has at most 500 lines and these twelve sections in order: Mission, Boundaries, Inputs, Tools, Safety rules, Procedure, Outputs, Acceptance criteria, When things go wrong, Red flags, References, Example. `tools/validate_skills.py` checks this.
- The skill calls its kernel as `python3 <skill-dir>/scripts/qa.py <command>`, always through the interpreter, where `<skill-dir>` is the directory that holds the SKILL.md. Write that form in every command the skill runs and in every command it gives the user. No host variable (such as `${CLAUDE_SKILL_DIR}`) in `SKILL.md`, no `§` section number anywhere under `skills/` or `agents/`, and no raw git command: git reads go through the skill's `scripts/git_read.py`.
- Never edit `scripts/qa.py` or `scripts/_kernel/` by hand. Change the shared kernel in `shared/kernel/` and run `python3 -I tools/vendor_kernel.py`; `--check` reports any drift.
- Each responsibility has exactly one owning skill in the catalog (`tools/check_catalog.py`). A new skill also goes into exactly one bundle in `.claude-plugin/marketplace.json`; `tools/check_packaging.py` checks the bundles and the entries' shared version.

## Evaluations

Skill text is written test-first, the way the existing skills were:

1. Write the evaluation cases (`skills/<name>/evals/*.json`) and their grader (`evals/grade.py`) before the skill text. Hard criteria are graded from the saved kernel state and file state of each run, not from the wording of the answer alone.
2. Run the cases without the skill (the baseline, `--config baseline`) through `tools/run_skill_eval.py` and keep the failing runs: they show what the skill has to change.
3. Write the skill, then run the same cases with it (`--config with-skill`). A case that misses gets at most two loophole rounds of text changes, each measured again.
4. Graders are frozen, that is committed, before the runs they grade. Any later grader change re-grades every saved run it applies to, and both numbers are reported. Read every answer an LLM judge graded yourself; the judge is never the only reader.

Never describe a run as passing unless it ran. Report the runs as measured, including the ones that missed.

Trigger cases for `claude plugin eval` live in `skills/<name>/evals/plugin/`. Edit only `prompts.json` there: at least 10 trigger and 10 near-miss prompts, each set with English and Spanish prompts (at least 4 of each, some in casual lowercase phrasing), near-misses aimed at a named sibling skill (mostly) or at none, and one boundary prompt with its criteria. No prompt names a skill, and no prompt or criterion holds a fixture name, a seed ID, answer-key text or a seed location. Then run `python3 -I tools/build_evals.py` to regenerate the case folders; never edit them by hand. CI runs `tools/build_evals.py --check`.

The evaluation key:

- Keep the API key in a file outside the repository and load it only inside the command that needs it. It is never printed, never committed, and the harnesses redact it from every output they save.
- `tools/run_plugin_eval.py` refuses any case that grants Bash, Write, Edit, NotebookEdit, WebFetch, WebSearch or an MCP tool, because `claude plugin eval` passes the key to its child sessions through the environment. The generated cases grant only Skill, Read, Glob and Grep.
- Every `claude` call a tool makes gets an environment built from scratch, with a temporary `HOME` and `CLAUDE_CONFIG_DIR` and telemetry and non-essential traffic turned off, so your own Claude Code configuration is never read. `claude plugin eval` always runs with `--no-publish`.
- Paid runs go through a cost ledger with a budget cap (`--ledger`, `--budget-usd`); a run with no end line or an unknown cost stops every later run until it is resolved.

The benchmark (`benchmark/`) has its own [README](benchmark/README.md). Its graders and rubrics are frozen before a benchmark run in the same way.

## Evidence

The rule for the project applies to contributions too. Never describe a test as passing unless it ran, and never invent results, coverage figures or compatibility claims. If you could not verify something, say so in the pull request.

## Pull requests

- Keep each pull request to one change, and add a test that fails without it.
- Use commit subjects in the form `type(scope): summary`, for example `fix(guard): ...`.
- Say what you ran and on which platform, and name any limitation.
- Sign off each commit with `git commit -s`. The sign-off certifies the [Developer Certificate of Origin](https://developercert.org).

## Design documents

The design documents are not published. Section references such as `§12.2` in code comments point to them, so treat the code and its tests as the public description of the behavior.
