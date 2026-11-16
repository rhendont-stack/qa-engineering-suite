"""Runs one benchmark scenario: the with-suite and baseline arms, interleaved, each run through ``tools/run_skill_eval.py``.

Usage: python3 -I benchmark/harness/run_benchmark.py --scenario PATH --arms with-suite,baseline --runs K --model M
       --judge-model J --budget-usd N --stage-cap-usd S --ledger PATH --out DIR

A scenario (``load_scenario``) is a JSON object with exactly these fields: ``id``, ``fixture``, ``answer_key`` (a file stem in
``benchmark/answer-keys/``), ``mode`` (``M<n>``), ``prompt``, ``grader`` (``honesty`` or ``audit``), ``bash_allow``,
``allow_write``, ``runtime_venv`` (a path or null), ``system_path_only``, ``keep_work``, ``timeout`` (seconds),
``tools_present`` and ``worst_run_usd`` (``{"with-suite": …, "baseline": …}``, each arm's projected largest run). A prompt that
names the repository's path or a seed ID (``F<n>-<nn>``) is refused. A run's harness case (``harness_case``) holds the id, the
prompt, the fixture and the run's allowances only, and is the same for both arms, which differ by ``--config`` alone: the
answer key, the mode, the grader, the tools present and the projections never reach the harness or the agent.

The runs take turns, one of each arm in the order ``--arms`` gives (with-suite, baseline, with-suite, …), until ``--runs`` of
each are done. Each is a ``run_skill_eval.py`` subprocess with ``--config <arm>``, the scenario's ``timeout`` and this command's
``--model``, ``--budget-usd``, ``--ledger`` and ``--out``; its environment holds ``PATH``, ``HOME``, ``TMPDIR`` and the key, each
when set, and nothing else. The harness writes each run's ``start`` and ``end`` lines in the ledger, under its lock, and the
``end`` line again in ``DIR/costs.jsonl``; this command writes no ledger line of its own.

Before each run the stage cap is checked: ``stage_spend`` (every run recorded in ``DIR/costs.jsonl``, earlier invocations on
the same ``DIR`` included) plus the arm's ``worst_run_usd``, or the largest run recorded there when that is larger, must not
pass ``--stage-cap-usd``. Otherwise the stage stops with status ``stage_cap_reached`` and exit 3, and each run not made is
recorded with the reason. A recorded cost that is unknown stops it the same way (``cost_unknown``). After each run, every
``start`` line it added to the ledger must have its ``end`` line there and in ``DIR/costs.jsonl``; otherwise the stage stops
(``open_run``, ``evidence_incomplete``; exit 1). A run the harness refuses (exit 3) or rejects as malformed (exit 2) stops the
stage with that code; a run that fails or times out is recorded, and the next one is checked against the stage cap as usual.

``DIR/runs.json`` holds the scenario, every run made (invocation, arm, index, run id, status, cost, the ``init`` tools and those
beyond the allow list, the staged plugin, the evidence directory, the harness's exit code and a redacted tail of its stderr) and
one entry per invocation (its arguments, status, stage spend, and the runs not made and why); it is rewritten after every run.
``DIR/cases/`` holds each arm's harness case. Grading is not part of this command yet, so each invocation records
``"graded": false``.

Exit codes: 0 every run finished ok; 1 a run failed or timed out, or a run has no end line or incomplete evidence; 2 bad
arguments; 3 refused (the stage cap, an unknown recorded cost, the ledger, or the harness refused a run); 130 interrupted.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tools" / "run_skill_eval.py"
ANSWER_KEYS = REPO / "benchmark" / "answer-keys"
ARMS = ("with-suite", "baseline")
GRADERS = ("honesty", "audit")
FIELDS = ("id", "fixture", "answer_key", "mode", "prompt", "grader", "bash_allow", "allow_write", "runtime_venv",
          "system_path_only", "keep_work", "timeout", "tools_present", "worst_run_usd")
NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
SEED_ID = re.compile(r"(?<![A-Za-z0-9])F\d{1,2}-\d{2}(?![0-9])")
STDERR_TAIL = 4000
CHILD_WAIT = 300       # seconds an interrupted harness gets to record its run before it is killed


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("qa_run_skill_eval", str(HARNESS))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_harness = _load_harness()
Ledger = _harness.Ledger
Refused = _harness.Refused
usage_error = _harness.usage_error
now_utc = _harness.now_utc
redact = _harness.redact
KEY_VAR = _harness.KEY_VAR


class Stop(Exception):
    """The stage stops: ``status`` says why, ``code`` is the exit code and ``detail`` goes with the first run not made."""

    def __init__(self, status: str, message: str, code: int, **detail: Any) -> None:
        super().__init__(message)
        self.status, self.code = status, code
        self.detail = dict(detail, message=message)


# -- the scenario -----------------------------------------------------------------------------------------------------

def _names_path(text: str, root: Path) -> bool:
    """Whether ``text`` names ``root``, or a path under it, in either spelling of it. Any character that cannot continue a
    path component bounds the match, so a path ending a sentence or set in quotes or brackets counts; a longer name that
    merely starts with it does not."""
    return any(re.search(r"(?<![\w.~/-])%s(?![\w-])" % re.escape(spelling), text)
               for spelling in {str(root), os.path.realpath(str(root))})


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _amount(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def load_scenario(path: Path) -> Dict[str, Any]:
    """The scenario at ``path``, checked field by field; ``SystemExit(2)`` for anything else."""
    try:
        scenario = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise usage_error("cannot read the scenario: %s" % exc)
    if not isinstance(scenario, dict) or set(scenario) != set(FIELDS):
        found = set(scenario) if isinstance(scenario, dict) else set()
        raise usage_error("a scenario holds exactly the fields %s; missing %s, unknown %s"
                          % (", ".join(FIELDS), sorted(set(FIELDS) - found), sorted(found - set(FIELDS))))
    prompt, key, venv = scenario["prompt"], scenario["answer_key"], scenario["runtime_venv"]
    checks = [
        (isinstance(scenario["id"], str) and NAME.match(scenario["id"]), "id must be a lower-case name"),
        (isinstance(scenario["fixture"], str) and scenario["fixture"].strip() and _harness.resolve(scenario["fixture"]).is_dir(),
         "fixture must be a fixture directory"),
        (isinstance(key, str) and NAME.match(key) and (ANSWER_KEYS / (key + ".json")).is_file(),
         "answer_key must be the stem of a file in benchmark/answer-keys/"),
        (isinstance(scenario["mode"], str) and re.fullmatch(r"M\d+", scenario["mode"]), "mode must be M<n>"),
        (isinstance(prompt, str) and prompt.strip(), "prompt must be a non-empty string"),
        (not isinstance(prompt, str) or not _names_path(prompt, REPO), "the prompt names the repository's path"),
        (not isinstance(prompt, str) or not SEED_ID.search(prompt), "the prompt names a seed ID"),
        (scenario["grader"] in GRADERS, "grader must be one of %s" % ", ".join(GRADERS)),
        (_strings(scenario["bash_allow"]) and not [p for p in scenario["bash_allow"] if len(p.split()) < 2
                                                    and p.split()[0] in _harness.FORBIDDEN_BARE],
         "bash_allow must list narrow prefixes, never a bare interpreter"),
        (isinstance(scenario["allow_write"], bool), "allow_write must be true or false"),
        (venv is None or (isinstance(venv, str) and venv.strip()), "runtime_venv must be a path or null"),
        (isinstance(scenario["system_path_only"], bool), "system_path_only must be true or false"),
        (_strings(scenario["keep_work"]), "keep_work must be a list of names"),
        (type(scenario["timeout"]) is int and scenario["timeout"] > 0, "timeout must be a positive number of seconds"),
        (_strings(scenario["tools_present"]), "tools_present must be a list of tool names"),
        (isinstance(scenario["worst_run_usd"], dict) and set(scenario["worst_run_usd"]) == set(ARMS)
         and all(_amount(v) for v in scenario["worst_run_usd"].values()),
         "worst_run_usd must give a positive amount for each of %s" % ", ".join(ARMS)),
    ]
    for passed, message in checks:
        if not passed:
            raise usage_error("%s: %s" % (path, message))
    return scenario


def harness_case(scenario: Mapping[str, Any], arm: str) -> Dict[str, Any]:
    """The ``run_skill_eval.py`` case of one run of ``arm``. It is the same for both arms, which differ by ``--config`` alone,
    and holds what the run needs and nothing a grader reads."""
    if arm not in ARMS:
        raise ValueError("unknown arm: %r" % arm)
    case: Dict[str, Any] = {"id": scenario["id"], "prompt": scenario["prompt"], "fixture": scenario["fixture"],
                            "bash_allow": list(scenario["bash_allow"]), "allow_write": scenario["allow_write"],
                            "system_path_only": scenario["system_path_only"]}
    if scenario["runtime_venv"]:
        case["runtime_venv"] = scenario["runtime_venv"]
    if scenario["keep_work"]:                # the harness takes a non-empty list or none
        case["keep_work"] = list(scenario["keep_work"])
    return case


# -- the stage --------------------------------------------------------------------------------------------------------

def _lines(path: Path) -> List[Any]:
    """The JSON value of each readable line of ``path``; a line that cannot be read is left out here, and
    ``_recorded_costs`` is what refuses it."""
    if not path.exists():
        return []
    found: List[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def _recorded_costs(out: Path) -> List[float]:
    """The cost of each run recorded in ``out/costs.jsonl``; ``Stop`` when one is unknown or a line cannot be read."""
    path = out / "costs.jsonl"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise Stop("cost_unknown", "%s cannot be read: %s" % (path, type(exc).__name__), 3)
    costs: List[float] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if not isinstance(record, dict) or record.get("cost_unknown") is not False or not _harness._is_amount(record.get("cost_usd")):
            raise Stop("cost_unknown", "%s line %d holds no known cost, so the stage's spend cannot be shown under its cap"
                       % (path, number), 3)
        costs.append(float(record["cost_usd"]))
    return costs


def stage_spend(out: Path) -> float:
    """What the runs recorded under ``out`` cost: the sum over ``out/costs.jsonl``, where the harness repeats each run's ``end``
    line. Raises ``Stop`` (``cost_unknown``) when a recorded cost is unknown."""
    return sum(_recorded_costs(out))


def check_stage(out: Path, worst: float, cap: float) -> None:
    """``Stop`` (``stage_cap_reached``) when the stage's spend plus the next run's projection passes ``cap``: the projection is
    the arm's ``worst`` or the largest run recorded in the stage, whichever is larger."""
    costs = _recorded_costs(out)
    spend, projected = sum(costs), max([worst] + costs)
    if round(spend + projected, 6) > cap:
        raise Stop("stage_cap_reached", "%.4f USD spent in the stage and %.4f USD projected for the next run pass the stage "
                   "cap of %.4f USD" % (spend, projected, cap), 3, stage_spend_usd=round(spend, 6), projected_usd=projected,
                   stage_cap_usd=cap)


def ledger_records(ledger: Any) -> List[Dict[str, Any]]:
    """Every ledger record, read under the ledger's lock; ``Stop`` when the ledger cannot be read."""
    try:
        with ledger.locked():
            return ledger.read()
    except (Refused, OSError) as exc:
        raise Stop("ledger_unreadable", str(exc), 3)


# -- one run ----------------------------------------------------------------------------------------------------------

def harness_env() -> Dict[str, str]:
    """The harness's environment: ``PATH``, ``HOME`` (the harness denies the real home to the agent), ``TMPDIR`` and the key,
    each when set, and nothing else. The harness builds the agent's own environment from scratch."""
    return {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", KEY_VAR) if os.environ.get(name)}


def run_child(command: List[str]) -> Tuple[int, str, str]:
    """Runs one harness in a session of its own; an interrupt here is passed on to it once, as SIGTERM, which the harness turns
    into an interrupted run with its end line, and is then raised again."""
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=harness_env(), start_new_session=True)
    try:
        out, err = proc.communicate()
    except BaseException:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=CHILD_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise
    return proc.returncode, out, err


def _refusal(stdout: str, stderr: str) -> str:
    for line in reversed(stdout.splitlines()):
        try:
            said = json.loads(line)
        except ValueError:
            continue
        if isinstance(said, dict) and isinstance(said.get("refused"), str):
            return said["refused"]
    return stderr.strip()[-STDERR_TAIL:]


def one_run(args: argparse.Namespace, scenario: Mapping[str, Any], arm: str, index: int, case_path: Path, out: Path,
            ledger: Any, number: int) -> Tuple[List[Dict[str, Any]], Optional[Stop]]:
    """One harness run of ``arm`` and what it recorded: ``(run entries, the reason the stage stops, or None)``. A run is the
    ``start`` line it added to the ledger; the entry takes the ``end`` line's fields."""
    before = {r["run_id"] for r in ledger_records(ledger) if r["event"] == "start"}
    command = [sys.executable, "-I", str(HARNESS), "--case", str(case_path), "--config", arm, "--model", args.model,
               "--budget-usd", repr(args.budget_usd), "--out", str(out), "--ledger", str(ledger.path),
               "--timeout", str(scenario["timeout"])]
    try:
        code, stdout, stderr = run_child(command)
    except KeyboardInterrupt:
        code, stdout, stderr = 130, "", "interrupted"
    records = ledger_records(ledger)
    ends = {r["run_id"]: r for r in records if r["event"] == "end"}
    copied = {r.get("run_id") for r in _lines(out / "costs.jsonl") if isinstance(r, dict)}
    key = os.environ.get(KEY_VAR, "")
    entries: List[Dict[str, Any]] = []
    for run_id in [r["run_id"] for r in records if r["event"] == "start" and r["run_id"] not in before]:
        end = ends.get(run_id, {})
        entries.append({"invocation": number, "arm": arm, "index": index, "run_id": run_id,
                        "status": end.get("status", "no_end_line"), "cost_usd": end.get("cost_usd"),
                        "cost_unknown": end.get("cost_unknown", True), "tools_listed": end.get("tools_listed"),
                        "tools_beyond_allow": end.get("tools_beyond_allow"), "skills_listed": end.get("skills_listed"),
                        "plugin": end.get("plugin"), "evidence": "runs/%s" % run_id, "harness_exit": code,
                        "harness_stderr": redact(stderr, key)[-STDERR_TAIL:]})
    open_runs = [e["run_id"] for e in entries if e["status"] == "no_end_line"]
    if code == 130:
        return entries, Stop("interrupted", "the run was interrupted", 130)
    if open_runs:
        return entries, Stop("open_run", "run %s has a start line and no end line in the ledger; its cost is unknown"
                             % ", ".join(open_runs), 1)
    missing = [e["run_id"] for e in entries if e["run_id"] not in copied]
    if missing:
        return entries, Stop("evidence_incomplete", "run %s is not in %s, so the stage's spend would be under-counted"
                             % (", ".join(missing), out / "costs.jsonl"), 1)
    if code == 3:
        return entries, Stop("refused", redact(_refusal(stdout, stderr), key), 3)
    if code == 2:
        return entries, Stop("harness_usage_error", redact(stderr.strip(), key)[-STDERR_TAIL:], 2)
    if code not in (0, 1) or not entries:
        return entries, Stop("harness_error", "the harness exited %s without recording a run" % code, 1)
    return entries, None


# -- the record -------------------------------------------------------------------------------------------------------

def load_record(out: Path, scenario_id: str, scenario_path: str) -> Dict[str, Any]:
    """``DIR/runs.json`` as an earlier invocation left it, or a new one; ``SystemExit(2)`` when it is another scenario's."""
    path = out / "runs.json"
    if not path.exists():
        return {"scenario": scenario_id, "scenario_path": scenario_path, "runs": [], "invocations": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise usage_error("%s cannot be read: %s" % (path, exc))
    if not isinstance(doc, dict) or doc.get("scenario") != scenario_id or not isinstance(doc.get("runs"), list) \
            or not isinstance(doc.get("invocations"), list):
        raise usage_error("%s belongs to another scenario or is not a benchmark record; use another --out" % path)
    return doc


def write_record(out: Path, doc: Mapping[str, Any]) -> None:
    path = out / "runs.json"
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(scratch), str(path))


def write_cases(out: Path, scenario: Mapping[str, Any], arms: Sequence[str]) -> Dict[str, Path]:
    folder = out / "cases"
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for arm in arms:
        paths[arm] = folder / ("%s-%s.json" % (scenario["id"], arm))
        paths[arm].write_text(json.dumps(harness_case(scenario, arm), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths


# -- main -------------------------------------------------------------------------------------------------------------

def parse(argv: Sequence[str]) -> argparse.Namespace:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:  # argparse exits 2 already; keep the message on stderr only
            print("error: " + message, file=sys.stderr)
            raise SystemExit(2)

    p = Parser(description="Run one benchmark scenario, its arms interleaved, under a stage cap.")
    for name in ("--scenario", "--arms", "--model", "--judge-model", "--ledger", "--out"):
        p.add_argument(name, required=True)
    p.add_argument("--runs", required=True, type=int)
    p.add_argument("--budget-usd", required=True, type=float)
    p.add_argument("--stage-cap-usd", required=True, type=float)
    args = p.parse_args(list(argv))
    args.arms = [arm.strip() for arm in args.arms.split(",")]
    if len(set(args.arms)) != len(args.arms) or [arm for arm in args.arms if arm not in ARMS]:
        raise usage_error("--arms is a comma-separated list of distinct arms among %s" % ", ".join(ARMS))
    if args.runs < 1 or not all(math.isfinite(v) and v > 0 for v in (args.budget_usd, args.stage_cap_usd)):
        raise usage_error("--runs, --budget-usd and --stage-cap-usd must be positive")
    return args


def _on_sigterm(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt()


def main(argv: Sequence[str]) -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)
    args = parse(argv)
    scenario = load_scenario(Path(args.scenario))
    out, ledger = Path(args.out), Ledger(Path(args.ledger))
    if os.path.realpath(str(out / "costs.jsonl")) == os.path.realpath(str(ledger.path)):
        raise usage_error("--out must not hold the ledger: the stage's costs.jsonl would be the ledger itself")
    doc = load_record(out, scenario["id"], args.scenario)
    cases = write_cases(out, scenario, args.arms)
    invocation: Dict[str, Any] = {
        "number": len(doc["invocations"]) + 1, "started": now_utc(), "ended": None, "arms": args.arms,
        "runs_per_arm": args.runs, "model": args.model, "judge_model": args.judge_model, "budget_usd": args.budget_usd,
        "stage_cap_usd": args.stage_cap_usd, "ledger": str(ledger.path), "status": "running", "stage_spend_usd": None,
        "not_run": [], "graded": False}
    doc["invocations"].append(invocation)
    order = [(arm, index) for index in range(1, args.runs + 1) for arm in args.arms]
    made: List[Dict[str, Any]] = []
    stop: Optional[Stop] = None
    for position, (arm, index) in enumerate(order):
        try:
            check_stage(out, scenario["worst_run_usd"][arm], args.stage_cap_usd)
            entries, stop = one_run(args, scenario, arm, index, cases[arm], out, ledger, invocation["number"])
        except Stop as before_the_run:
            entries, stop = [], before_the_run
        except KeyboardInterrupt:
            entries, stop = [], Stop("interrupted", "interrupted between runs", 130)
        made += entries
        doc["runs"] += entries
        write_record(out, doc)
        if stop is not None:
            rest = order[position + 1:] if entries else order[position:]
            invocation["not_run"] = [{"arm": a, "index": i, "reason": stop.status} for a, i in rest]
            if invocation["not_run"]:
                invocation["not_run"][0].update(stop.detail)
            print("%s: %s: %s" % ("refused" if stop.code == 3 else "stopped", stop.status, stop), file=sys.stderr)
            break
    # The graders and the aggregation are wired in here once they are frozen: benchmark/graders/grade_run.py for each run
    # in doc["runs"] that has evidence, then metrics.aggregate over the grades and doc["runs"]. Until then "graded" stays false.
    try:
        spend: Optional[float] = round(stage_spend(out), 6)
    except Stop:
        spend = None
    invocation.update(status=stop.status if stop else "complete", ended=now_utc(), stage_spend_usd=spend)
    write_record(out, doc)
    print(json.dumps({"scenario": scenario["id"], "status": invocation["status"], "runs": len(made),
                      "not_run": len(invocation["not_run"]), "stage_spend_usd": spend, "record": str(out / "runs.json")},
                     sort_keys=True))
    if stop is not None:
        return stop.code
    return 0 if all(entry["harness_exit"] == 0 for entry in made) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
