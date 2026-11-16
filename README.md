# QA Engineering Suite

> **Status: pre-release, not ready for use.** The evidence kernel and all 16 skills exist. An install path for Claude Code exists (a plugin marketplace with the full suite, two bundles and an opt-in guard plugin), and it is not yet verified on any host. No version has been released.

An open-source suite of [Agent Skills](https://agentskills.io) meant to let an AI agent act as an independent QA team. Its rule is simple: a result counts only when reproducible evidence backs it. A test that did not run is never reported as passing.

## What exists today

| Part | Path | State |
|---|---|---|
| `qa` kernel: Python 3.9 or later, standard library only | `shared/kernel/qa/`, `shared/launcher/qa.py` | Built and tested |
| JSON Schemas, gate profiles, command-guard rules | `shared/schemas/`, `shared/policies/` | Built and tested |
| Test suite and repository checks | `tests/`, `tools/` | Built |
| Spine skills: `qa-orchestrator`, `qa-test-execution`, `qa-codebase-intelligence`, `qa-reporting`, and three Claude Code subagents | `skills/`, `agents/` | Built, evaluated on a pilot basis; see the [milestone 2 report](docs/reports/milestone-2.md) |
| Analysis and change skills: `qa-requirements`, `qa-test-strategy`, `qa-test-generation`, `qa-regression-guard`, `qa-mutation` | `skills/` | Built, evaluated on a pilot basis; see the [milestone 3 report](docs/reports/milestone-3.md) |
| Domain skills: `qa-api-testing`, `qa-database`, `qa-security`, `qa-performance` | `skills/` | Built, evaluated on a pilot basis with the tools out of the evaluated agent's reach; the tools themselves measured by deterministic legs in containers; see the [milestone 4a report](docs/reports/milestone-4a.md) |
| Fixtures F2 (an orders API), F3 (database migrations) and F6 (a training lab, loopback only, run only in containers), with answer keys | `fixtures/`, `benchmark/` | Built |
| Container runs (`qa run --container`) | `shared/kernel/qa/container.py` | Built, tested against a remote engine |
| Domain skills: `qa-frontend`, `qa-release`, `qa-mobile` (static only) | `skills/` | Built, evaluated on a pilot basis with the tools out of the evaluated agent's reach; the browser and pipeline tools measured by deterministic legs in containers; see the [milestone 4b report](docs/reports/milestone-4b.md) |
| Fixtures F1 (a static web front end), F10 (Android and iOS configuration, no app build) and F11 (an inert pipeline with seeded workflow weaknesses), with answer keys | `fixtures/`, `benchmark/` | Built |
| CI re-execution jobs (`qa ci begin`, `qa ci generate`) | `shared/kernel/qa/ci.py` | Built and tested; the generated job has not yet run on a hosted runner |
| Claude Code plugin marketplace: the full suite, the `qa-core` and `qa-specialists` bundles, and the opt-in `qa-command-guard` plugin | `.claude-plugin/marketplace.json`, `plugins/qa-command-guard/` | Built; each entry installed from a local export into a scratch configuration; not verified on a host; see the [milestone 5a report](docs/reports/milestone-5a.md) |
| `claude plugin eval` cases: 10 trigger, 10 near-miss and 1 boundary prompt per skill, in English and Spanish, and a budgeted runner | `skills/*/evals/plugin/`, `tools/build_evals.py`, `tools/run_plugin_eval.py` | Built; the trigger and near-miss cases run once for two skills, the boundary cases once for all 16 |
| Benchmark harness, graders and LLM judge | `benchmark/` | Built; a pilot of two scenarios at three runs per arm; see [benchmark/README.md](benchmark/README.md) |
| Manifests for hosts other than Claude Code | none | Not started; only after a host is verified |

The kernel records each execution in a hash-chained ledger and classifies commands before running them. It requires a recorded authorization for risky activities and ingests JUnit, CTRF and SARIF reports, k6 summaries and axe-core results (each violation an alert tagged with its WCAG success criterion), and records LCOV, Cobertura and JaCoCo coverage, mutation-testing reports and Lighthouse results as measurements that never decide a status. It also records requirements, acceptance criteria, risks and test cases with the links between them, computes a traceability matrix with its gaps, records fixes and revalidates them (FAIL_TO_PASS and PASS_TO_PASS), runs a change in a suite worktree, records the files the suite writes, tracks findings, evaluates release gates, renders reports (Markdown, JSON, SARIF and a self-contained HTML page) and checks the claims those reports make. `qa ci` pins a CI workspace to the accepted configuration and writes a GitHub Actions job, as a patch, that re-runs recorded host runs.

## Install

Nothing in this section is verified yet. Every channel below is **expected** to work and will be verified in Phase 4 (milestone 6): installed through the channel, the skills listed by the host, trigger evaluations, and one M0 and one M3 scenario, recorded with the host version, the model and the date. So far each marketplace entry has been installed only from a local export of this repository into a scratch Claude Code configuration (see the [milestone 5a report](docs/reports/milestone-5a.md)). The version is a pre-release, `0.1.0-dev.1`. Every skill needs `python3` 3.9 or later on `PATH`.

| Channel | In Claude Code | What gets installed | State |
|---|---|---|---|
| Plugin marketplace | `/plugin marketplace add rhendont-stack/qa-engineering-suite`, then `/plugin install qa-engineering-suite@qa-engineering-suite` | All 16 skills and 3 subagents | Expected until Phase 4 |
| Bundles from the same marketplace | `/plugin install qa-core@qa-engineering-suite` or `/plugin install qa-specialists@qa-engineering-suite` | 8 skills each (everyday change and fix verification, or the domain specialists); each bundle also loads the 3 subagents | Expected until Phase 4 |
| Guard plugin (opt-in) | `/plugin install qa-command-guard@qa-engineering-suite` | One `PreToolUse` hook that checks Bash commands while a QA workspace is active; no skills. See its [README](plugins/qa-command-guard/README.md) | Expected until Phase 4 |
| Copy | Copy the `skills/qa-*` folders of a clone into `~/.claude/skills/` (all projects) or `.claude/skills/` (one project) | Skills only | Expected until Phase 4 |

The CLI's own plugin validator names a one-step form for Claude Code 2.1.275 or later, which adds the marketplace and installs the entry in one command. Like every line in this section, it is expected until Phase 4:

```text
/plugin install qa-engineering-suite --marketplace rhendont-stack/qa-engineering-suite
/plugin install qa-core --marketplace rhendont-stack/qa-engineering-suite
/plugin install qa-specialists --marketplace rhendont-stack/qa-engineering-suite
/plugin install qa-command-guard --marketplace rhendont-stack/qa-engineering-suite
```

Install the full plugin or the bundles, not both. The two bundles share no skills, but both load the three subagents, so installing both bundles puts each subagent under two plugin names; the full plugin holds every skill and subagent, so adding it to a bundle puts those under two names as well. Each marketplace entry currently installs the whole repository, about 24 MB, so a bundle is not smaller on disk than the full plugin.

### Compatibility matrix

**Verified** means installed, triggered and run through scenarios in Phase 4, with the host version, the model and the date. **Expected** means the skills follow the Agent Skills specification and the host's documented format, but nothing has been run on that host.

| Host | Channel | Verified | Expected |
|---|---|---|---|
| Claude Code | Plugin marketplace, bundles and guard plugin | | Yes |
| Claude Code | Copy into `~/.claude/skills/` or `.claude/skills/` | | Yes |
| Other hosts that read Agent Skills from a skills directory | Copy into the host's skills directory (for example `.agents/skills/`) | | Yes, skills only |

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

Milestone 3 adds the following. Its run IDs, evidence IDs, grader changes and limitations are in the [milestone 3 report](docs/reports/milestone-3.md).

| Check | Result |
|---|---|
| Fixture F5 through the whole chain (requirements to report), one run | Records, written tests with red checks, runs, gate `NOT_READY` and a report listing every traceability gap; all nine seeded issues are in its records (one of them, the empty cart, identified but not confirmed by a run). Graded fail on one criterion by the frozen grader, which missed the report's Markdown-escaped gap kinds; pass on every hard criterion after that grader was fixed and the run re-graded offline |
| F7 at C1, review of a "feature done" commit through the orchestrator (`change` template), one run | Regression step done, the regression and the weakened test recorded, gate `NOT_READY` |
| F7 at C3, verification of a fix through the orchestrator (`fix` template), exit run and one re-run | The exit run did not finish: a helper call and the orchestrator's own launcher call, both written relative to a worktree, were outside the evaluation's allow-list and refused, so no revalidation and no gate were recorded. After the final review fixes (the skills return to the project root after a worktree `cd`, and the allow-lists carry those forms) one re-run held every hard criterion: revalidation passed, gate `READY` under profile `fix` |
| Final review fix wave, three runs per scenario on the final texts of the five changed skills | Every hard criterion in 40 of 42 runs (12 of 14 scenarios 3 of 3; `qa-regression-guard` verify-fix and `qa-mutation` coverage pressure 2 of 3 each) |
| F7 at C4 and C5, deterministic, no agent | Special-cased fix and runner hook each recorded as a test-quality finding; gate `NOT_READY` with `test_integrity` not met |
| Kernel mutation score, six modules, one container run | 84.93 over 4890 mutants (79.42 to 95.79 per module); `report/check` unmeasured |
| Kernel tests on Linux x86_64 in containers through the kernel, 3.9 and 3.15 | First legs failed on one grader test that lacked a `git` guard in images without git; after a test-only guard both re-runs passed (4094 passed, 896 skipped, 0 failed; 868 of the skips need `git`, which the images lack). These legs ran before the final review fix wave and were not re-run; the kernel evidence after the wave is the remote full suite on Python 3.9 and 3.15 |
| Kernel tests on macOS arm64 for milestone 3 | Not run locally. The macOS CI job at the final commit concluded `success` on 3.9 and 3.15 (5068 pass, 26 skip, 0 fail each) |

Milestone 4a adds the following. Its run IDs, evidence IDs, grader changes and limitations are in the [milestone 4a report](docs/reports/milestone-4a.md).

| Check | Result |
|---|---|
| Deterministic legs on F2, F3 and F6, in containers with network `none` on a remote engine | F2: Spectral, two Schemathesis runs, Hurl, a ZAP active scan and two k6 runs (p95 21.192 ms and 20.747 ms, both thresholds held); recall 7 of 9 over the five tools. F3: squawk (converted to SARIF) and SQLite and PostgreSQL migration roundtrips; recall 2 of 2 for squawk and the plan, the two legs the exit driver scored; 4 of 7 with the SQLite roundtrip scored afterwards by file only (an upper bound), 2 of 7 under the recall grader's rules as written. F6: Gitleaks, Semgrep, a Unicode scan and two ZAP plans against the lab as the run's own loopback service; recall 10 of 10. No OSV-Scanner leg ran |
| One evaluated whole-suite run per fixture | F3 and F6 held every hard criterion; F2 missed one, `no_unbacked_pass_rate`, on a proposed load objective that the grader read as a pass rate (a suspected grader false positive). No external host contacted, no host run of the lab, every tool leg the agents started recorded as unable to start and none claimed |
| Evaluations of the four new skills, three counted runs per scenario | `qa-api-testing` 7 of 9, `qa-database` 9 of 9, `qa-security` 8 of 9, `qa-performance` 9 of 9 |
| Kernel mutation score, `report/check`, one container run | 69.95 over 396 mutants (277 killed, 119 survived); the stored report parsed after milestone 3's redaction fix |
| Kernel tests on Linux x86_64 in containers through the kernel, 3.9 and 3.15 | First legs failed 4 tests each, all test-side (a corpus trace check that read the container's `/work` path as a machine path, and a grader test without a `git` guard); after a test-only fix both re-runs passed (6082 tests: 4977 pass, 1105 skip, 0 fail; 1043 of the skips need `git`, which the images lack) |
| Kernel tests on macOS arm64 for milestone 4a | Not run locally; left to the macOS CI job |

Milestone 4b adds the following. Its run IDs, evidence IDs, grader changes and limitations are in the [milestone 4b report](docs/reports/milestone-4b.md).

| Check | Result |
|---|---|
| Deterministic legs on F1, F10 and F11, in containers with network `none` on a remote engine | F1: a static UI scan, Playwright checks at three viewports (42 tests: 14 pass, 16 fail, 12 skip), axe-core and Lighthouse on two pages, with F1's server as the run's own loopback service; recall 8 of 8 over the four tools. F10: the static configuration check found all four seeds and neither negative control; the device check was refused without a grant, as the static-only design intends; recall 4 of 4. F11: workflow scan, release check, actionlint and zizmor `--offline`; recall 7 of 7 |
| The generated CI job, once, in a private GitHub repository | The local host run, the generated workflow and its actionlint and zizmor lint all held. GitHub first did not start the one workflow run's job (an account billing condition). Its one re-run, after that was cleared, ran on a hosted runner, re-ran the recorded command with the same 7 passing tests, and its bundle came back and matched the local run |
| One evaluated whole-suite run per fixture | Each run had all 16 skills installed and loaded only the domain skill matching its task (`qa-frontend`, `qa-mobile`, `qa-release`; the prompts name no skill), never `qa-orchestrator`. F1, F10 and F11 each held every hard and secondary criterion of the frozen exit grader, and of its claim criteria as amended after the review (re-graded, same verdicts): no external host, no install or download, no release action, every tool leg the agent started recorded as unable to start and none claimed; recall 8 of 8, 4 of 4 and 7 of 7 |
| Evaluations of the three new skills, three counted runs per scenario | `qa-frontend`, `qa-mobile` and `qa-release` 9 of 9 each on their final texts, each after a confirmation set; `qa-database`, re-run after the trust rule, 6 of 6 after one loophole round |
| Kernel mutation score, `gate`, one container run | `gate` 85.07 over 2211 mutants (1873 killed, 8 timed out, 277 survived, 53 without tests), after the criteria milestones 4a and 4b added to it; `ci` unmeasured, because the mutation tool has no `ci` module |
| Kernel tests on Linux x86_64 in containers through the kernel, 3.9 and 3.15 | The first leg (3.9) failed 16 tests, all test-side (two parametrized grader tests without a `git` guard); after a test-only fix both legs passed (7205 tests: 5947 pass, 1258 skip, 0 fail; 1176 of the skips need `git`, which the images lack) |
| Kernel tests on macOS arm64 for milestone 4b | Not run locally. The macOS CI job on the pull request, at `2ed31ec`, concluded `success` on 3.9 and 3.15 (7177 pass, 88 skip, 0 fail each) |

Milestone 5a adds the following. Its run IDs, costs and limitations are in the [milestone 5a report](docs/reports/milestone-5a.md).

| Check | Result |
|---|---|
| Each marketplace entry installed with the Claude Code CLI 2.1.321 in a scratch configuration, from a local export, at three commits | Every inventory as expected: the full suite 16 skills and 3 subagents, each bundle 8 skills and the 3 subagents, the guard plugin one hook. Each root-source entry installs the whole export (24,193,202 bytes at `0181af3`, the last commit the report measured) |
| The skills' new launcher form (`python3 <skill-dir>/scripts/qa.py`), one evaluation case per skill | 15 of 16 cases held every hard criterion; the orchestrator's case did so in 1 of 5 runs (two runs where it did not trigger, two that skipped the reporting step before a text fix); one of the five, which crossed local midnight and lost its grants, was ruled not valid evidence, so 1 of the 4 valid runs |
| `claude plugin eval` smoke, one run per case | Trigger and near-miss cases 40 of 40 for `qa-api-testing` and `qa-performance`; boundary cases 9 of 16 on each case's latest run, with six skills not loaded at all |
| Pilot benchmark, F9 (honesty) and F2 (audit), three runs per arm with and without the suite | No safety violation in 12 of 12 runs. F9: each command's status reported correctly in 3 of 3 runs with the suite and 1 of 3 without; F2: the suite was installed but did not load in any run. Runs with no unbacked claim: F9 0 of 3 with the suite and 1 of 3 without, F2 0 of 3 with and 2 of 3 without (one run with the suite not computable). Projected cost of the full benchmark: 55.91 US$ expected, 81.84 US$ worst |
| CI with the pinned reference validator `skills-ref` and the packaging checks | On the pull request, at `10cc0c9`, every job concluded `success`: `skills-ref` accepted all 16 skills, and the suite ran on each of the four legs with 0 fail (7555 to 7558 tests pass, 86 to 89 skipped). The report lists the earlier runs and what they caught |

GitHub Actions runs the suite on Linux x86_64 and macOS arm64, with both Python versions, on every push and pull request. It is also configured to run the packaging checks and the pinned reference validator `skills-ref` on every skill. The milestone 5a report gives their result on its pull request, and the Actions tab shows the latest result.

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
| 3 | `qa-requirements`, `qa-test-strategy`, `qa-test-generation`, `qa-regression-guard`, `qa-mutation` | Built; exit evidence has open items, see the milestone 3 report |
| 4a | `qa-api-testing`, `qa-database`, `qa-security`, `qa-performance`; k6 ingestion; fixtures F2, F3 and F6 | Built; exit evidence has open items, see the milestone 4a report |
| 4b | `qa-frontend`, `qa-release`, `qa-mobile`; HTML reports, axe-core and Lighthouse ingestion, `qa ci`; fixtures F1, F10 and F11 | Built; exit evidence has open items, see the milestone 4b report |
| 5a | Packaging (plugin marketplace, bundles, opt-in guard plugin), `claude plugin eval` cases for every skill, benchmark harness and graders, pilot benchmark | Built; exit evidence has open items, see the milestone 5a report |
| 5b | The orchestrator's `audit` and `release` templates and the other items carried from 4b; the pilot's findings (the audit judge's format, the safety grader's file-state input, skill descriptions that did not trigger) | Planned |
| 6 | Full benchmark, internal audit, documentation, v0.1.0 release candidate | Planned |

A skill that fabricates a claim, violates a safety rule, misreports a status or follows an injected instruction will be held back from the release. A skill that only misses a softer target ships marked `experimental`, with its measured numbers published.

## Known limits

- **The command guard is defense in depth, not a security boundary.** It cannot see commands inside scripts or opaque programs. The [milestone 1 report](docs/reports/milestone-1.md) lists the classes it does not cover.
- **The kernel does not sandbox a host run.** Only `qa run --container` isolates the program under test, and only as far as the container engine does.
- **Mutation testing of the kernel is partial.** One mutmut run in a Linux container measured six core modules: canonical 95.79, compare 81.34, gate 84.64, ledger 94.54, status 94.57 and trace 79.42 (4145 killed and 8 timed out of 4890 mutants, 84.93 over the six). A second run, after the redaction fix that had left milestone 3's `report/check` report unreadable, measured `report/check` at 69.95 (277 killed of 396 mutants, 119 survived), the lowest of the seven. A third run, in milestone 4b, re-measured `gate` after the criteria milestones 4a and 4b added to it: 85.07 (1873 killed and 8 timed out of 2211 mutants, 277 survived, 53 without tests). Other kernel modules, `ci` among them, are not measured. Tests that start the kernel in a subprocess run the unmutated copy, so the surviving counts are an upper bound. See the milestone 3, 4a and 4b reports.
- **The orchestrator's delegation is not yet reliable in every run.** Milestone 3 writes every launcher call literally and dispatches subagents in the foreground. Its fix-verification exit run stopped when the agent, still inside a suite worktree, wrote calls in a relative form the evaluation's allow-list did not cover; after the final review fixes, the skills say to return to the project root after a worktree `cd`, and before the fixes the review scenario held every hard criterion in 2 of 3 runs on the earlier final text, and on the final texts the review and fix-verification scenarios each held every hard criterion in 3 of 3 runs, plus one exit re-run. One `qa-regression-guard` run still stayed in a worktree and had a call refused. Skills that write their own input files run in the orchestrator's own context, because the subagents cannot write files; most of those delegation rows have no evaluated run. See the milestone 3 report.
- **No install channel is verified yet.** The marketplace entries have been installed only from a local export into a scratch configuration. A channel counts as verified only once it has been installed on a host through that channel, the host lists the skills, the trigger evaluations and one M0 and one M3 scenario have run there, and the host version, the model and the date are recorded; no host has done that yet (see the [milestone 5a report](docs/reports/milestone-5a.md)).
- **The pilot benchmark is two scenarios at *k* = 3.** F9 (honesty) and F2 (audit), three runs per arm. In F2 the suite was installed but never loaded, so F2 measured its description, not its audit. The thresholds are proposals, and none is claimed met.
- **Trigger accuracy is measured for two skills only.** `qa-api-testing` and `qa-performance`, one run per prompt. In the boundary cases, which ask a skill to skip one of its safeguards, six skills were not loaded at all.
- **A marketplace install carries the whole repository.** Each entry installs the full export, fixtures, benchmark and every skill's evaluation cases included (about 24 MB), so the bundles are not smaller on disk.
- **Grants expire at the end of the local day by default.** A long run that crosses midnight loses its grants partway.
- **Evaluation results are small and in-sample.** Three runs per scenario, with descriptions tuned on the same prompts; the milestone 3, 4a and 4b exit runs are one run each, and the fix-verification scenario has one re-run.
- **Evaluated agents cannot reach the container engine.** The evaluations of the seven domain skills measure static work, the checks the skills write, refusals and honesty with the tools absent. The tools themselves are measured only by the deterministic legs. See the milestone 4a and 4b reports.
- **`qa-mobile` is static only.** It reads Android and iOS configuration source files; manifest merging, build variants and signed entitlements are out of its reach. Dynamic checks are listed with a remediation and never run: the suite does not install an SDK or an emulator, or drive a device.
- **The CI job re-runs host runs only, and has run on a hosted runner once.** Container runs are left out with a reason (milestone 5b), and the job carries no environment values. In milestone 4b its one run, re-run once on `ubuntu-24.04`, matched the local run for one fixture; a run where the runner and the local run disagree has not been exercised on a runner.
- **`rollback_validated` needs an ingested rehearsal.** The suite never deploys, so the release gate's rollback criterion stays without evidence until a rehearsal record from the project's own pipeline arrives through `qa ingest`.
- **The frontend tools run only in the container engine.** Playwright, axe-core and Lighthouse run inside the pinned `playwright` image from a committed lockfile; with no engine, `qa-frontend` records each browser leg as blocked and keeps its findings suspected.
- **Screenshots are kept without baseline comparison.** Each viewport's screenshot is kept as its sha256, size and viewport in the JUnit report, not as a stored image, and there is no visual-regression baseline yet (milestone 5b).
- **The command guard reads ZAP Automation Framework plans by their path, not their content.** A plan edited at the baseline plan's path reads as passive. `qa-security` writes its plans from committed assets, and the kernel does not yet hash a run's inputs.
- **Fixture F6 is a deliberately vulnerable training lab.** It starts only on a loopback address and answers only loopback Host names; the suite runs it only inside a container, as a run's own service with network `none`. Do not run it on a host.
- **Coverage XML with a DOCTYPE** must be in an ASCII-compatible encoding, and a literal `&` in its comments or CDATA is refused.
- **Evidence IDs in the report** resolve against local ledgers that are kept out of git, so a fresh clone cannot resolve them. An extra `--ledger` that verifies proves only that its hash chain is internally consistent, not where it came from.
- **An evidence bundle carries the configured engine string.** `qa config set container.engine` records the value in the ledger, and `qa report bundle` ships the ledger, so an `ssh <alias> docker` engine puts that host alias in every bundle. Use an alias you are willing to share before you export one.
- **Repository git configuration is neutralised, not trusted.** Before each kernel git call, the repository's own value of these keys is replaced by your user-level value, or blanked when you have none: filter and diff programs; `lfs.extension.<name>.clean|smudge`, `lfs.customtransfer.<name>.path|args` and `lfs.standalonetransferagent` (which only selects an agent by name); `credential.helper` (also per URL), `core.sshCommand`, `core.askPass` and `remote.<name>.vcs`. Git keeps the first value of `remote.<name>.uploadpack`, `remote.<name>.receivepack` and `core.gitProxy`, and a repository's value comes before a command-line one, so a blank cannot replace it: while the repository sets one of these, every kernel git call refuses to run (reads report `git_unavailable`; `qa worktree add` and `qa worktree remove` report `git_config_refused` with the key names; `qa run --container` cannot list the project's files and reports `project_unlistable`); remove the key from the repository's configuration to continue. The same keys in your own git config do not cause a refusal. Submodules are never entered, terminal prompts are off (`GIT_TERMINAL_PROMPT=0`), and lazy fetch of missing partial-clone objects is switched off with `GIT_NO_LAZY_FETCH=1` on a git that supports it (2.44 or later, detected by running git). On an older git, a partial clone (`extensions.partialClone`, or a `remote.<name>.promisor` that is not explicitly false) is refused in the same way, because a checkout or a diff there could fetch missing objects. A value in your own user-level git config (for example git-lfs or a credential helper) still applies. `qa worktree add` and `qa worktree remove` use this same session. Not enforced: other repository keys that can name a program (for example `diff.external`, `core.pager`, or `url.<base>.insteadOf` rewrites to a helper transport) are not blanked, although no kernel git call uses a pager or an external diff, and a repository's `.gitattributes` can still select a driver you configured. Whether a real git-lfs honours the overrides the kernel passes is untested, because the tests use a stand-in for git-lfs. This needs git 2.26 or later (`git config --show-scope`); with an older git every kernel git read reports `git_unavailable` and fails closed.
- **A container run cannot start inside a suite worktree.** Its archive would hold the main working tree, so the kernel refuses it (`container_in_worktree`) instead of recording a commit that was not tested.
- **The design documents are not published.** Section references such as `§12.2` in code comments and in the report point to them.

## Project documents

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)

## License

[Apache License 2.0](LICENSE).
