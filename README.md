# QA Engineering Suite

> **Status: pre-release, not ready for use.** The evidence kernel and the four spine skills exist. Twelve of the 16 skills have not been built, there is no install path, and no version has been released.

An open-source suite of [Agent Skills](https://agentskills.io) meant to let an AI agent act as an independent QA team. Its rule is simple: a result counts only when reproducible evidence backs it. A test that did not run is never reported as passing.

## What exists today

| Part | Path | State |
|---|---|---|
| `qa` kernel: Python 3.9 or later, standard library only | `shared/kernel/qa/`, `shared/launcher/qa.py` | Built and tested |
| JSON Schemas, gate profiles, command-guard rules | `shared/schemas/`, `shared/policies/` | Built and tested |
| Test suite and repository checks | `tests/`, `tools/` | Built |
| Spine skills: `qa-orchestrator`, `qa-test-execution`, `qa-codebase-intelligence`, `qa-reporting`, and three Claude Code subagents | `skills/`, `agents/` | Built, evaluated on a pilot basis; see the [milestone 2 report](docs/reports/milestone-2.md) |
| Container runs (`qa run --container`) | `shared/kernel/qa/container.py` | Built, tested against a remote engine |
| The other 12 skills | none yet | Not started |
| Packaging and host manifests | none yet | Not started |

The kernel records each execution in a hash-chained ledger and classifies commands before running them. It requires a recorded authorization for risky activities and ingests JUnit, CTRF and SARIF reports. It also tracks findings, evaluates release gates, renders reports and checks the claims those reports make.

## Verification so far

The kernel's test suite has passed on these platforms:

| Platform | Python |
|---|---|
| macOS arm64 | 3.9 and 3.15 |
| Linux arm64, in containers | 3.9 and 3.15 |
| Linux x86_64, on GitHub Actions | 3.9 and 3.15 |

Counts, skipped tests and limitations of the local runs are in the [milestone 1 report](docs/reports/milestone-1.md).

Milestone 2 adds the following. Its numbers, evidence IDs and limitations are in the [milestone 2 report](docs/reports/milestone-2.md).

| Check | Result |
|---|---|
| Targeted review of fixture F4 through the orchestrator | One run, verdict `NOT_READY`, ledger and report checks clean |
| Targeted review of fixture F9 through the orchestrator | Two runs. A prompt asking for a release made the orchestrator ask for a mode and record nothing; a re-run that asked for a review of the change reached a gate, `NOT_READY`, which is correct for the whole fixture (one of its runners records a genuine failure) |
| Scenario F9-01 alone (the absent tool), deterministic, no agent | Gate `INSUFFICIENT_EVIDENCE` under profile `release`, as the answer key expects |
| Fixture F8 in a container with no network | Two runs, isolation `container`, network `none` |
| Kernel tests on Linux x86_64 in containers, 3.9 and 3.15 | First runs failed on tests that need `git` in an image without it; after a test-only guard both re-runs passed (3234 passed, 309 skipped, 0 failed) |
| Kernel tests on macOS arm64 for milestone 2 | Not run locally; left to the macOS CI job |

GitHub Actions runs the suite on Linux x86_64 and macOS arm64, with both Python versions, on every push and pull request. The Actions tab shows the latest result.

## Try the kernel

The kernel is usable from a clone, for evaluation only. Clone this repository, then run these from the root of a git repository of your own:

```bash
python3 /path/to/qa-engineering-suite/shared/launcher/qa.py init
```

```bash
python3 /path/to/qa-engineering-suite/shared/launcher/qa.py doctor
```

`init` creates a `.qa/` directory in your project. Every command prints one JSON object on stdout.

A new project starts as `untrusted`. In that state `qa run` records a command on the host as not authorized and does not run it. `qa run --container IMAGE@DIGEST` runs it instead in a container with no network and no bind mount, from an image already on the engine. The output of `qa run` says how to do either.

## Roadmap

| Milestone | Scope | State |
|---|---|---|
| 1 | Kernel foundations | Done |
| 2 | `qa-orchestrator`, `qa-test-execution`, `qa-codebase-intelligence`, `qa-reporting`; container runs | Built; exit evidence has open items, see the milestone 2 report |
| 3 | `qa-requirements`, `qa-test-strategy`, `qa-test-generation`, `qa-regression-guard`, `qa-mutation` | Planned |
| 4 | `qa-api-testing`, `qa-database`, `qa-security`, `qa-frontend`, `qa-performance`, `qa-release`, `qa-mobile` | Planned |
| 5 | Packaging, per-skill evaluations, pilot benchmark | Planned |
| 6 | Full benchmark, internal audit, documentation, v0.1.0 release candidate | Planned |

A skill that fabricates a claim, violates a safety rule, misreports a status or follows an injected instruction will be held back from the release. A skill that only misses a softer target ships marked `experimental`, with its measured numbers published.

## Known limits

- **The command guard is defense in depth, not a security boundary.** It cannot see commands inside scripts or opaque programs. The [milestone 1 report](docs/reports/milestone-1.md) lists the classes it does not cover.
- **The kernel does not sandbox a host run.** Only `qa run --container` isolates the program under test, and only as far as the container engine does.
- **Mutation testing of the kernel** is deferred to milestone 3.
- **The orchestrator's delegation is fragile under background dispatch.** Subagent launcher calls written with shell variables are denied there, and the fix is not yet applied or measured. See the milestone 2 report.
- **Evaluation results are small and in-sample.** Three runs per scenario, with descriptions tuned on the same prompts.
- **Evidence IDs in the report** resolve against local ledgers that are kept out of git, so a fresh clone cannot resolve them. An extra `--ledger` that verifies proves only that its hash chain is internally consistent, not where it came from.
- **An evidence bundle carries the configured engine string.** `qa config set container.engine` records the value in the ledger, and `qa report bundle` ships the ledger, so an `ssh <alias> docker` engine puts that host alias in every bundle. Use an alias you are willing to share before you export one.
- **Repository git configuration is neutralised, not trusted.** Before each kernel git call, the repository's own value of these keys is replaced by your user-level value, or blanked when you have none: filter and diff programs; `lfs.extension.<name>.clean|smudge`, `lfs.customtransfer.<name>.path|args` and `lfs.standalonetransferagent` (which only selects an agent by name); `credential.helper` (also per URL), `core.sshCommand`, `core.askPass` and `remote.<name>.vcs`. Git keeps the first value of `remote.<name>.uploadpack`, `remote.<name>.receivepack` and `core.gitProxy`, and a repository's value comes before a command-line one, so a blank cannot replace it: while the repository sets one of these, every kernel git call refuses to run (reads report `git_unavailable`, `qa worktree add` reports `git_config_refused` with the key names); remove the key from the repository's configuration to continue. The same keys in your own git config do not cause a refusal. Submodules are never entered, terminal prompts are off (`GIT_TERMINAL_PROMPT=0`), and lazy fetch of missing partial-clone objects is switched off with `GIT_NO_LAZY_FETCH=1` on a git that supports it (2.44 or later, detected by running git). On an older git, a partial clone (`extensions.partialClone` or a promisor remote) is refused in the same way, because a checkout or a diff there could fetch missing objects. A value in your own user-level git config (for example git-lfs or a credential helper) still applies. `qa worktree add` and `qa worktree remove` use this same session. Not enforced: other repository keys that can name a program (for example `diff.external`, `core.pager`, or `url.<base>.insteadOf` rewrites to a helper transport) are not blanked, although no kernel git call uses a pager or an external diff, and a repository's `.gitattributes` can still select a driver you configured. Whether a real git-lfs honours the overrides the kernel passes is untested, because the tests use a stand-in for git-lfs. This needs git 2.26 or later (`git config --show-scope`); with an older git every kernel git read reports `git_unavailable` and fails closed.
- **A container run cannot start inside a suite worktree.** Its archive would hold the main working tree, so the kernel refuses it (`container_in_worktree`) instead of recording a commit that was not tested.
- **The design documents are not published.** Section references such as `§12.2` in code comments and in the report point to them.

## Project documents

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)

## License

[Apache License 2.0](LICENSE).
