# Milestone 1 exit evidence

Date of the runs: 2026-11-08. Evidence commit: `dabb361002f34a45e8f7c48733c6f1a150ec735e` on branch `milestone-1/kernel-foundations`, clean tree. This report's own commit follows that one and changes only this file. `<repo>` stands for the repository path.

Every `EXE-` and `AUTH-` ID below resolves against the local `.qa/` ledger. D8 keeps that ledger out of git, so a fresh clone has nothing to resolve them against.

## Exit evidence

Spec §18 asks for "L1 green on 3.9 + newest (macOS, Linux); deterministic scenarios in §16.3 pass". L1 here excludes mutation testing of the kernel (deviation 24). Each row is the latest attempt of its leg, as `qa run` recorded it. The numeric columns are the recorded `tests` fields.

| Leg | Execution | Status | Strength | Total | Pass | Fail | Error | Skip | Commit |
|---|---|---|---|---|---|---|---|---|---|
| macOS arm64, CPython 3.9.6 | EXE-01M6YC1Y999Q0Q89817S0WDW9S | passed | structured | 2225 | 2220 | 0 | 0 | 5 | `dabb361` |
| macOS arm64, CPython 3.15.2 | EXE-01M6YC9TB3EK3K25RW4BFA5Q30 | passed | structured | 2225 | 2220 | 0 | 0 | 5 | `dabb361` |
| Linux arm64 container, `python:3.9-slim` | EXE-01M6YCF4G3EZSTZJ5E6000GW09 | passed | structured | 2225 | 2156 | 0 | 0 | 69 | `dabb361` |
| Linux arm64 container, `python:3-slim` (CPython 3.15) | EXE-01M6YCPEKZAMAC0ZWQ0JR2V1M6 | passed | structured | 2225 | 2156 | 0 | 0 | 69 | `dabb361` |

Each leg recorded exit code 0 and a clean tree, and each ran alone, macOS first. `qa ledger verify` reported no problems after the last leg.

The §16.3 scenarios are the nine tests in `tests/integration/test_scenarios.py`:

- On macOS, nine of nine passed in EXE-01M6YC1Y999Q0Q89817S0WDW9S and EXE-01M6YC9TB3EK3K25RW4BFA5Q30.
- On Linux, seven of nine passed in EXE-01M6YCF4G3EZSTZJ5E6000GW09 and EXE-01M6YCPEKZAMAC0ZWQ0JR2V1M6. The other two skip for lack of git (see Limitations), so those two scenarios have only macOS evidence.

The Linux images are referenced by digest:

- `python:3.9-slim`: `python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b`
- `python:3-slim`: `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928`

Both indexes were checked with `docker buildx imagetools inspect` on 2026-11-08. The wheels pip selected carry the tags `cp39` and `cp315`.

The Linux test command was:

```
docker run --rm --cpus=2 --memory=2g --init --network=none -e PYTHONDONTWRITEBYTECODE=1 -v <repo>:/src:ro -v qa-venv-<tag>:/venv -v {evidence}:/out <image> /venv/bin/python -m pytest -q -p no:cacheprovider -c /src/pytest.ini --rootdir /src --junitxml=/out/junit.xml /src/tests
```

## Earlier attempts

These attempts ran on commit `eba154d`, before the test fix in `dabb361`. None is the exit evidence for its leg.

| Leg | Execution | Status | Reason | Strength | Total | Pass | Fail | Error | Skip |
|---|---|---|---|---|---|---|---|---|---|
| macOS 3.9.6 | EXE-01M6YB3T1J6BE9EWKHY8MW67EG | passed | none | structured | 2225 | 2220 | 0 | 0 | 5 |
| macOS 3.15.2 | EXE-01M6YB8XKYYQB5Q24Q93E6Q36G | passed | none | structured | 2225 | 2220 | 0 | 0 | 5 |
| Linux `python:3.9-slim` | EXE-01M6YBQR3VZSS58K0WKP64PCYK | failed | assertion | structured | 2225 | 2154 | 4 | 0 | 67 |

That Linux attempt, EXE-01M6YBQR3VZSS58K0WKP64PCYK, ran without `--init`. Its four failing tests were:

- `tests/unit/test_execution.py::test_two_runs_without_git_count_as_equal_and_keep_their_limitation` and `::test_truncation_and_git_limitations` expected `not_a_git_repo`, which the kernel can report only where git exists. The images ship no git, so the kernel's `git_unavailable` was correct. Commit `dabb361` adds the `needs_git` guard to both tests.
- `tests/unit/test_runner.py::test_timeout_kills_process_group` and `::test_background_child_holding_stdout` found the grandchild process still answering `os.kill(pid, 0)`. The later Linux legs add `--init`, and both tests passed in EXE-01M6YCF4G3EZSTZJ5E6000GW09 and EXE-01M6YCPEKZAMAC0ZWQ0JR2V1M6. The inferred cause is that pytest as PID 1 did not reap the orphan. The process state itself was not inspected.

The Linux installs under `qa run --activity install-dependencies`, one per image, are:

| Image | Execution | Status | Strength | Commit |
|---|---|---|---|---|
| `python:3.9-slim` | EXE-01M6YBPYJFRKGHWT6TPPVFZS89 | passed | exit_code | `eba154d` |
| `python:3-slim` | EXE-01M6YCNP3NAMGDPF2FTF9FWJ50 | passed | exit_code | `dabb361` |

The 3.9 environment was kept in its volume and reused on `dabb361`.

## Limitations

- **Attempts that did not succeed.** One, EXE-01M6YBQR3VZSS58K0WKP64PCYK, failed with reason `assertion`, as described above.
- **Tests the Linux legs skip for lack of git.** 59 tests on each Linux leg, EXE-01M6YCF4G3EZSTZJ5E6000GW09 and EXE-01M6YCPEKZAMAC0ZWQ0JR2V1M6, with the same set on both. The images ship no git. These tests have no Linux evidence:

```
tests.integration.test_scenarios (2)
  test_f9_missing_tool_blocks_and_gate_insufficient
  test_gate_truth_table_via_cli
tests.unit.test_execution (6)
  test_attempt_needs_the_same_commit
  test_attempt_needs_the_same_diff
  test_attempt_on_the_same_commit_and_diff_runs
  test_git_facts_are_read_before_the_run
  test_truncation_and_git_limitations
  test_two_runs_without_git_count_as_equal_and_keep_their_limitation
tests.unit.test_gate (26)
  test_a_passing_rerun_cannot_hide_a_failed_root_end_to_end
  test_a_run_on_the_dirty_tree_being_evaluated_counts
  test_a_run_that_leaves_a_git_ignored_file_counts
  test_a_run_that_leaves_an_untracked_file_says_what_to_do
  test_a_scan_as_the_latest_execution_does_not_count_end_to_end
  test_accepted_risk_conditions_then_expiry
  test_advisory_no_evidence_is_not_a_condition
  test_an_edit_after_the_run_reads_no_evidence_under_change
  test_an_expired_or_false_positive_acceptance_is_no_accepted_risk
  test_an_explicit_target_commit_does_not_compare_the_tree
  test_an_explicit_target_is_recorded
  test_an_explicit_target_records_no_tree
  test_change_profile_ready_after_passing_suite
  test_conditions_list_advisory_not_met_in_policy_order_then_risks_by_id
  test_deleting_a_session_after_evaluation_is_detected
  test_evaluation_is_deterministic
  test_exit_code_strength_is_rejected_under_release
  test_failing_suite_is_not_ready
  test_missing_tool_gives_insufficient_evidence
  test_open_high_finding_blocks_with_detail
  test_override_via_propose_accept_is_not_drift
  test_tampered_evidence_fails_the_gate_end_to_end
  test_the_default_target_is_the_current_commit
  test_the_event_records_the_hash_of_the_tree_it_evaluated
  test_unevaluable_criteria_say_why
  test_with_an_explicit_target_clean_tree_still_governs
tests.unit.test_gitinfo (16)
  test_binary_change_changes_diff_hash
  test_clean_repo
  test_detached_head_has_no_branch
  test_fsmonitor_hook_not_executed
  test_git_cannot_start
  test_git_missing
  test_git_timeout
  test_no_commit
  test_not_a_repo
  test_qa_config_never_changes_diff_hash
  test_qa_dir_never_dirties
  test_tracked_change_changes_diff_hash
  test_untracked_content_changes_diff_hash
  test_untracked_fifo_does_not_hang
  test_untracked_symlink_hashed_by_target
  test_untracked_symlink_to_dev_zero_does_not_hang
tests.unit.test_tools (9)
  test_a_file_with_a_non_ascii_name_is_listed_and_read
  test_a_git_ignored_file_is_not_read
  test_a_listed_file_deleted_from_the_tree_is_skipped
  test_a_listed_file_whose_directory_became_a_file_is_skipped
  test_a_tracked_and_an_untracked_file_are_both_read
  test_a_tracked_symlink_to_a_fifo_is_neither_read_nor_reported
  test_check_repo_passes_on_this_repository
  test_the_walk_would_have_read_what_git_ignores
  test_without_git_on_the_path_the_walk_runs_instead
```

- **Other Linux skips.** Ten more on each Linux leg, for two reasons. The containers run as root, so file and directory modes do not bind (8 tests in `test_cli`, `test_doctor`, `test_execution` and `test_tools`). The file system also tells names apart by case and by normalization form, which leaves 2 macOS-specific tests in `test_execution` without a Linux run.
- **macOS skips.** Five on each macOS leg, because APFS refuses file names that are not UTF-8: the four tests in the next item, and `test_tools.py::test_output_survives_a_file_name_that_is_not_utf8`.
- **Non-UTF-8 name handling is Linux-only evidence.** These four tests in `tests/unit/test_execution.py` passed on both Linux legs, EXE-01M6YCF4G3EZSTZJ5E6000GW09 and EXE-01M6YCPEKZAMAC0ZWQ0JR2V1M6, as read from their stored JUnit files, and skip on macOS:
  - `test_a_cwd_utf8_cannot_encode_is_refused_on_a_filesystem_that_allows_it`
  - `test_the_recorded_cwd_is_escaped_when_a_symlink_leads_to_such_a_name`
  - `test_a_stored_report_name_is_escaped_on_a_filesystem_that_allows_it`
  - `test_an_ingested_report_name_is_escaped_on_a_filesystem_that_allows_it`
- **Mutation testing of the kernel.** Deferred to milestone 3 (deviation 24).
- **CI workflow.** The 2x2 matrix in `.github/workflows/ci.yml` has never run, because nothing is pushed. Its cells are `ubuntu-latest` (x86_64) and `macos-latest` (arm64), each with Python 3.9 and 3.15, plus an `l0` job on `ubuntu-latest` with 3.15. The only check so far is actionlint 1.7.12.
- **Linux architecture.** The Linux legs ran on arm64 only, in local containers limited to 2 CPUs and 2 GB of memory. The CI cells are x86_64.
- **What the ledger's environment fields describe.** The `environment` and `isolation` fields of each `execution.recorded` event describe the macOS host and the interpreter that launched `qa`, not the interpreter or container under test. They read `darwin-arm64`, Python 3.9.6 and `host` on all nine executions, including the CPython 3.15 and Linux legs, so they differ from the row labels above. Each leg's operating system and interpreter are established by its recorded command (the virtual environment path or the image digest) and by the D4 installs below. For the macOS 3.15 leg, that recorded path is the only ledger link to its interpreter, and no stored artifact of that leg records the version.
- **Network use inside Docker.** The two Linux installs, EXE-01M6YBPYJFRKGHWT6TPPVFZS89 and EXE-01M6YCNP3NAMGDPF2FTF9FWJ50, reached PyPI from inside the container, where the guard cannot see it. Milestone 2's `--container` runs make container network policy explicit. The test legs used `--network=none`.

## Approvals

Trust, `config.changed` event `EVT-01M6YB38AXZFCR76P5E0KFGHXF`, quoted as recorded: `User reply "Aprobar todo" (2026-11-07) to "¿Apruebas la traducción, el plan, las instalaciones D4, git init y marcar el repo como confiable?"`

Install authorization, AUTH-001, activity `install-dependencies`, environment `local`, valid for 2026-11-08 only, quoted as recorded: `User reply "Autorizo registrarlo (Recomendado)" (2026-11-08) to the question on registering install-dependencies for today only: hash-pinned pytest and jsonschema in disposable Docker containers, new volumes qa-venv-39 and qa-venv-315, and pulling the Python 3.15 image`

## D4 installs

Measured on 2026-11-08 with `du -sk`, `docker image inspect --format '{{.Size}}'` and `docker system df -v`, except where a row says otherwise. Nothing was installed globally. Everything below can be removed.

| Item | What it is | Size |
|---|---|---|
| `.venv39` | Hash-pinned dev environment on the system CPython 3.9.6 (pytest 8.4.2, jsonschema 4.25.1) | 19040 KB |
| `.venv315` | Hash-pinned dev environment on CPython 3.15.2 (pytest 8.4.2, jsonschema 4.26.0) | 31744 KB |
| `.venv-semgrep` | Semgrep 1.184.0 on Python 3.12.15, from wheels with recorded sha256 | 309936 KB |
| `.tools` | actionlint 1.7.12, checksum and SLSA provenance verified | 5696 KB |
| Homebrew `python@3.15` 3.15.2 | Unlinked after install, so `python3` is still the system one. The install also added `openssl@4` and upgraded `ca-certificates`, `sqlite` and `lz4` | about 200 MB, from the install record |
| Docker image `python:3.9-slim` | `python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b` | 208539147 bytes |
| Docker image `python:3-slim` | `python@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928` | 207427676 bytes |
| Docker image Maven | `maven@sha256:9673eef9ed503146591667ffb92eaaa22f1b8899ed60d9df0df4da3f3a1214a6`, used for Surefire output in the corpus | no longer present when measured |
| Docker volume `qa-venv-39` | Linux 3.9 test environment | 32.65 MB |
| Docker volume `qa-venv-315` | Linux 3.15 test environment | 28.03 MB |

The Vitest, CTRF reporter and Gradle JUnit 5 producers ran in temporary directories that were deleted afterwards, so they are not installed.

## Deviations

The milestone's deviations from the specification are numbers 11 to 24 in §22.3 of `docs/proposal/phase-2-architecture.md`. They are cited here by number and not restated.
