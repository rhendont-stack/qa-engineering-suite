# Contributing

The project is pre-release and only the kernel exists. The checklist for writing a skill will be added when the first skills are built. Please read the [code of conduct](CODE_OF_CONDUCT.md) first, and report security problems as described in the [security policy](SECURITY.md).

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

## Evidence

The rule for the project applies to contributions too. Never describe a test as passing unless it ran, and never invent results, coverage figures or compatibility claims. If you could not verify something, say so in the pull request.

## Pull requests

- Keep each pull request to one change, and add a test that fails without it.
- Use commit subjects in the form `type(scope): summary`, for example `fix(guard): ...`.
- Say what you ran and on which platform, and name any limitation.
- Sign off each commit with `git commit -s`. The sign-off certifies the [Developer Certificate of Origin](https://developercert.org).

## Design documents

The design documents are not published. Section references such as `§12.2` in code comments point to them, so treat the code and its tests as the public description of the behavior.
