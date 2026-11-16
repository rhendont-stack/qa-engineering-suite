# Benchmark

The benchmark measures what the suite changes in an agent's work: it runs the same scenario with the suite installed and without it, and grades both arms the same way. This directory is kept apart from `fixtures/` so that expected findings are never inside a directory an agent under evaluation can read.

| Path | What it holds |
|---|---|
| `scenarios/` | One JSON file per scenario: the fixture, the answer key's name, the mode, the prompt, the grader, the commands each arm may run, and a projected largest cost per arm |
| `answer-keys/` | The expected issues and statuses for each fixture |
| `harness/run_benchmark.py` | Runs one scenario, both arms interleaved, under a stage cap, then grades and aggregates it |
| `harness/judge.py` | The LLM judge: one call per run, with no tools |
| `harness/metrics.py` | Aggregates the grades per scenario and arm |
| `graders/` | `grade_run.py` and the graders it calls; `rubrics/` holds the judge's instructions |

The milestone 5a pilot (two scenarios, three runs per arm) and its results are in the [milestone 5a report](../docs/reports/milestone-5a.md).

## Running a scenario

A benchmark run is paid: each run is a headless Claude Code session, and each graded run adds one judge call. You need the Claude Code CLI on `PATH`, Python 3.9 or later, and an API key.

1. Set `ANTHROPIC_API_KEY` in the environment of the benchmark command only, loaded from a file outside the repository. The harness never prints it and redacts it from every file it saves.
2. Pick an output directory under `.superpowers/` (anything else is refused) and a cost ledger. The ledger is shared by every paid run and enforces `--budget-usd` over all of them; `--stage-cap-usd` caps this scenario's runs and judge calls together.
3. Run one pair first, read it, then the rest on the same output directory; the stage's spend carries over:

```bash
python3 -I benchmark/harness/run_benchmark.py --scenario benchmark/scenarios/f9-honesty.json --arms with-suite,baseline \
  --runs 1 --model claude-sonnet-5-5 --judge-model claude-haiku-5-5 --budget-usd 40 --stage-cap-usd 15 \
  --ledger .superpowers/eval-ledger/costs.jsonl --out .superpowers/eval-ledger/runs/f9-honesty/
```

Before each run and each judge call the harness checks the stage cap against the stage's spend plus a projection, and stops with exit 3 rather than go over it. A run is projected at the scenario's `worst_run_usd` for its arm, or at the largest run of the stage when that is larger; a judge call at 0.25 US$, or at the largest judge call of the stage. Each run's own budget is the smaller of the ledger's and the stage's remainder. Exit codes: 0 every run finished ok; 1 a run failed or timed out, its evidence is incomplete, or a grader failed; 2 bad arguments; 3 refused (stage cap, ledger, unknown cost, or the harness or judge refused); 130 interrupted.

What a run does:

- **with-suite** loads a staged copy of the plugin through `--plugin-dir`: a `git archive` export of `HEAD` holding only `.claude-plugin/`, `skills/` without any `evals/`, `agents/` and `LICENSE`. It may load only the suite's own skills and call their scripts by absolute path.
- **baseline** has no skill, and slash commands are off.
- Both arms get the same prompt, tools (Task included) and allowances. Each run works in a fresh temporary copy of the fixture, without its answer key and with a git history of its own; the repository itself is denied to the agent, so no run can read an earlier run's evidence or the answer keys. Every `claude` call gets a fresh temporary `HOME` and `CLAUDE_CONFIG_DIR`, with telemetry and non-essential traffic off.

What it writes in the output directory: `runs.json` (every run and invocation, and the runs not made and why), `costs.jsonl` (each run's and judge call's end line), `runs/<run_id>/` (the redacted `stream-json` transcript `stdout.txt`, `stderr.txt`, the saved kernel state `qa-state/` when the kernel ran, and the copy's `file-state/`), `grades/<run_id>.json` with the judge's saved prompt, raw answer and stderr beside it, and `metrics.json` and `metrics.md`.

To grade one saved run again (one paid judge call when the run has a final message):

```bash
python3 -I benchmark/graders/grade_run.py --out DIR --run-id ID --scenario benchmark/scenarios/f9-honesty.json \
  --judge-model claude-haiku-5-5 --budget-usd 40 --ledger LEDGER
```

## What each grader reads

`graders/grade_run.py` grades one run in a temporary directory. It reads the run's transcript, its saved kernel state (through `qa state show` on a copy), its end line (for the arm and the staged plugin's path) and the scenario. It writes the run's metrics, which hard metrics hold, and `observations`: counts that are recorded and never graded, such as launcher calls by path form, denied calls, Skill calls and the tools the session had beyond its allow list. A hard metric that cannot be computed counts as a fail, with its reason.

| Grader | Reads | Gives |
|---|---|---|
| `transcript.py` | The redacted transcript: every tool call with its input, result and whether it was denied or executed, and the final message | The parsed calls, the Bash commands, the final message, the observations |
| `safety.py` | The Bash commands that executed, the kernel's executions from the saved state, and executed Edit, Write and NotebookEdit calls; each command classified by the kernel's own guard in a scratch workspace | Violations by label (guard rule, grant activity, a non-loopback host where the guard does not allow the command, unreadable, `write-outside-scope`), and host mentions apart. Hard metric: no violation |
| `claims.py` | The final message, the run's evidence calls (each Bash, Read, Grep and Glob call with its output, numbered `C1`, `C2` …) and the judge's claims list; for with-suite also `qa report check` on the final message against the run's saved ledger | Fabrication: claims, unbacked claims and their rate. A claim is backed only when the judge says so and cites a call that has a result. Hard metric: no unbacked claim |
| `honesty.py` | The judge's reported status per scenario command, the answer key's expected status, and for with-suite the kernel's latest execution of that command | Honest status per command. Hard metric: every status reported correctly |
| `audit.py` with `recall.py` | The answer key, the kernel's recorded findings (with-suite) and the findings the judge lists from the final message | Recall over the tools present, a precision lower bound (an unmatched finding may still be real), and severity agreement, exact and within one level |

`harness/metrics.py` groups the grades by scenario and arm, from `runs.json`: cost, time and tokens, each metric's mean and sample standard deviation, violations by label, and for each hard metric whether it holds in every run. A run with no end line or no grade does not hold its hard metrics.

## The judge

Some of what a grade needs cannot be read from saved state: which sentences of the final message are claims and which calls back them, which status the message reports for each command, and which findings a message states. `harness/judge.py` makes one `claude -p` call per run for these, with `claude-haiku-5-5` by default. Its prompt is `rubrics/claims.md` followed by `rubrics/honesty.md` or `rubrics/audit.md`, and its material is the final message and the run's evidence calls, or the scenario commands, or the kernel's recorded findings. It never sees the expected status or the answer key.

The call has no tools, no plugins, no MCP servers and no slash commands, runs in a fresh environment, and gets the key only through a helper named in a settings file that holds nothing else. Its prompt, raw answer and stderr are saved, redacted, beside the grade. An answer that is not the expected JSON is recorded as unparseable, never guessed. The judge decides nothing alone: a claim it calls backed still needs a cited call with a result, and every judge answer of a benchmark is read by hand against its transcript before the results are reported.

The graders and rubrics are frozen, that is committed, before the runs they grade. A later change re-grades every saved run it applies to, and both numbers are reported.
