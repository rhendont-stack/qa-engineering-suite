# QA Engineering Suite

> **Status: pre-release, not ready for use.** Only the evidence kernel exists. None of the 16 skills has been built, there is no install path, and no version has been released.

An open-source suite of [Agent Skills](https://agentskills.io) meant to let an AI agent act as an independent QA team. Its rule is simple: a result counts only when reproducible evidence backs it. A test that did not run is never reported as passing.

## What exists today

| Part | Path | State |
|---|---|---|
| `qa` kernel: Python 3.9 or later, standard library only | `shared/kernel/qa/`, `shared/launcher/qa.py` | Built and tested |
| JSON Schemas, gate profiles, command-guard rules | `shared/schemas/`, `shared/policies/` | Built and tested |
| Test suite and repository checks | `tests/`, `tools/` | Built |
| The 16 skills | none yet | Not started |
| Packaging and host manifests | none yet | Not started |

The kernel records each execution in a hash-chained ledger and classifies commands before running them. It requires a recorded authorization for risky activities and ingests JUnit, CTRF and SARIF reports. It also tracks findings, evaluates release gates, renders reports and checks the claims those reports make.

## Verification so far

The kernel's test suite has passed on these platforms:

| Platform | Python |
|---|---|
| macOS arm64 | 3.9 and 3.15 |
| Linux arm64, in containers | 3.9 and 3.15 |

Counts, skipped tests and limitations are in the [milestone 1 report](docs/reports/milestone-1.md). The GitHub Actions workflow has not run yet, and Linux x86_64 has no evidence so far.

## Try the kernel

The kernel is usable from a clone, for evaluation only. Clone this repository, then run these from the root of a git repository of your own:

```bash
python3 /path/to/qa-engineering-suite/shared/launcher/qa.py init
```

```bash
python3 /path/to/qa-engineering-suite/shared/launcher/qa.py doctor
```

`init` creates a `.qa/` directory in your project. Every command prints one JSON object on stdout.

A new project starts as `untrusted`. In that state `qa run` records the command as not authorized and does not run it, because container runs do not exist yet. The output of `qa run` says how to approve a change of trust level.

## Roadmap

| Milestone | Scope | State |
|---|---|---|
| 1 | Kernel foundations | Done |
| 2 | `qa-orchestrator`, `qa-test-execution`, `qa-codebase-intelligence`, `qa-reporting`; container runs | Planned |
| 3 | `qa-requirements`, `qa-test-strategy`, `qa-test-generation`, `qa-regression-guard`, `qa-mutation` | Planned |
| 4 | `qa-api-testing`, `qa-database`, `qa-security`, `qa-frontend`, `qa-performance`, `qa-release`, `qa-mobile` | Planned |
| 5 | Packaging, per-skill evaluations, pilot benchmark | Planned |
| 6 | Full benchmark, internal audit, documentation, v0.1.0 release candidate | Planned |

A skill that fabricates a claim, violates a safety rule, misreports a status or follows an injected instruction will be held back from the release. A skill that only misses a softer target ships marked `experimental`, with its measured numbers published.

## Known limits

- **The command guard is defense in depth, not a security boundary.** It cannot see commands inside scripts or opaque programs. The [milestone 1 report](docs/reports/milestone-1.md) lists the classes it does not cover.
- **The kernel does not sandbox anything.** Isolation from the program under test arrives with container runs in milestone 2.
- **Mutation testing of the kernel** is deferred to milestone 3.
- **Evidence IDs in the report** resolve against a local ledger that is kept out of git, so a fresh clone cannot resolve them.
- **The design documents are not published.** Section references such as `§12.2` in code comments and in the report point to them.

## Project documents

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)

## License

[Apache License 2.0](LICENSE).
