"""Runs one skill's ``claude plugin eval`` cases on an exported copy of the suite, under the evaluation ledger's budget.

Usage: python3 -I tools/run_plugin_eval.py --skill NAME [--case GLOB] [--runs N] --model MODEL [--judge-model MODEL]
       --budget-usd N --ledger PATH --out DIR [--commit REV]

The plugin under test is a ``git archive`` export of ``REV`` (default ``HEAD``) at ``<tmp>/qa-engineering-suite/``, never
the working tree, so the plugin's name is the suite's. The export is staged before the run, in scratch only:

- the skill's cases (``skills/<skill>/evals/plugin/``, without ``prompts.json`` or any ``results/``) are copied to
  ``plugin-evals/<skill>/`` at the export's root, which is passed as ``--eval-dir``. The CLI refuses an eval directory
  inside the plugin's ``skills/`` (a loaded component directory) and refuses an absolute one, so the cases cannot sit
  under ``skills/`` or outside the export;
- every ``skills/*/evals/`` is then removed from the export, so a loaded skill's directory holds no case, grader or
  harness material;
- each staged case's ``runs:`` is set to ``--runs``: the CLI makes ``--runs`` runs but reports the frontmatter's count as
  ``runsPerCase``, and a result whose ``runsPerCase`` differs from ``--runs`` is recorded as ``runs_mismatch``;
- ``.claude-plugin/plugin.json`` is written as ``{"name": "qa-engineering-suite"}`` when the export has none: without a
  root manifest the CLI resolves no plugin from the export (deviation 99).

``check_cases`` refuses the run (exit 3) when any case of the skill, selected by the glob or not, grants ``Bash``,
``Write``, ``Edit``, ``NotebookEdit``, ``WebFetch``, ``WebSearch``, an ``mcp__`` tool, or a grant it cannot read, in
``prompt.md``'s frontmatter or in ``case.yaml``. The CLI passes the key to its children through the environment, so a
case must not be able to run or send anything.

Every ``claude`` call gets an environment built from scratch: ``PATH``, a temporary ``HOME`` and ``CLAUDE_CONFIG_DIR``,
``DISABLE_TELEMETRY=1`` and ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``, plus ``ANTHROPIC_API_KEY`` from the caller's
environment for the paid call only (the ``--help`` probe gets no key). Nothing else is copied from the caller.

The budget authority is the ledger (``--ledger``), shared with ``tools/run_skill_eval.py`` and read under its exclusive
lock: a ``start`` line before the run and an ``end`` line the moment it finishes, before any evidence is written. A
``start`` with no ``end``, or an ``end`` with an unknown cost, refuses every later run. The run's cap (``--max-cost-usd``)
is the budget left over the whole ledger. The cost is the result's ``costUsd`` (judge calls included); a missing or
unreadable ``costUsd``, a missing result and an interrupted run are recorded as unknown, never as 0.

Verdicts come from each run's grader results, never from the result's ``casesPassed``, which counts every case scoring at
least ``--threshold`` (0 here). ``DIR`` receives ``result.json`` and ``results/`` (the CLI's), ``cli-stdout.txt``,
``cli-stderr.txt``, ``summary.json`` (per case: runs, passes, fails, and each run's grader results) and ``costs.jsonl``
(the ``end`` line again). Then every file under ``DIR`` is scanned and the key's forms (``redact_bytes``) are replaced by
the marker; symbolic links are not followed. ``DIR`` must not already hold a result.

Exit codes: 0 the run finished and was recorded with status ``ok``; 1 it was recorded with another status (``failed``,
``partial``, ``runs_mismatch``, ``timeout``, ``interrupted``) or its evidence was not fully saved; 2 bad arguments; 3
refused (a forbidden grant, a CLI without an essential flag, no key, the budget reached or an unknown recorded cost).
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def _load_harness() -> Any:
    path = Path(__file__).resolve().parent / "run_skill_eval.py"
    spec = importlib.util.spec_from_file_location("qa_run_skill_eval", str(path))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_harness = _load_harness()
Ledger = _harness.Ledger
Refused = _harness.Refused
check_budget = _harness.check_budget
run_cap = _harness.run_cap
append_line = _harness.append_line
redact_bytes = _harness.redact_bytes
key_forms = _harness.key_forms
now_utc = _harness.now_utc

REPO = Path(__file__).resolve().parent.parent
PLUGIN_NAME = "qa-engineering-suite"
CASES_ROOT = "plugin-evals"
DEFAULT_JUDGE = "claude-haiku-5-5"
KEY_VAR = "ANTHROPIC_API_KEY"
WALL_TIMEOUT = 7200
FORBIDDEN_TOOLS = frozenset({"Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"})
# The flags without which a run could publish, overspend or wait on a prompt: the CLI's help must list each.
ESSENTIAL_FLAGS = ("--no-publish", "--max-cost-usd", "--trust-plugin", "--eval-dir", "--output-dir", "--json")
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
GRANT_LINE = re.compile(r"^(\s*)allowed_tools\s*:\s*(.*?)\s*$")


def usage_error(message: str) -> "SystemExit":
    print("error: " + message, file=sys.stderr)
    return SystemExit(2)


def refuse(message: str) -> int:
    print(json.dumps({"refused": message}, sort_keys=True))
    print("refused: %s" % message, file=sys.stderr)
    return 3


# -- the cases --------------------------------------------------------------------------------------------------------

def _frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 3)
    return text[4:end + 1] if end >= 0 else ""


def _grants(text: str, where: str) -> List[str]:
    """Every ``allowed_tools`` entry in a YAML text: an inline list, a block list or a single scalar. A form this cannot
    read is refused, because an unread grant cannot be shown to be safe."""
    lines = text.splitlines()
    found: List[str] = []
    for number, line in enumerate(lines):
        match = GRANT_LINE.match(line)
        if not match:
            continue
        indent, value = len(match.group(1)), match.group(2)
        if value.startswith("["):
            if not value.endswith("]"):
                raise Refused("%s: allowed_tools is not a one-line list; refusing a grant that cannot be read" % where)
            found += [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        elif value:
            found.append(value.strip("'\""))
        else:
            for following in lines[number + 1:]:
                if not following.strip():
                    continue
                if len(following) - len(following.lstrip()) <= indent and not following.lstrip().startswith("- "):
                    break
                if not following.lstrip().startswith("- "):
                    raise Refused("%s: allowed_tools holds a line that is not a list item; refusing a grant that cannot "
                                  "be read" % where)
                found.append(following.lstrip()[2:].strip().strip("'\""))
    return found


def _is_case(path: Path) -> bool:
    return path.is_dir() and ((path / "prompt.md").is_file() or (path / "case.yaml").is_file())


def check_cases(eval_dir: Path, case_glob: Optional[str]) -> List[Path]:
    """The case folders under ``eval_dir`` whose name matches ``case_glob`` (all of them for None), sorted. Raises
    ``Refused`` when any case, selected or not, grants a forbidden tool or a grant that cannot be read, or when the
    directory holds a symbolic link."""
    selected: List[Path] = []
    for root, dirs, files in os.walk(str(eval_dir), followlinks=False):
        base = Path(root)
        for name in dirs + files:
            if (base / name).is_symlink():
                raise Refused("%s: a symbolic link in the eval directory; refusing" % (base / name).relative_to(eval_dir))
        if base == eval_dir or not _is_case(base):
            continue
        rel = str(base.relative_to(eval_dir))
        texts = []
        if (base / "prompt.md").is_file():
            texts.append((_frontmatter((base / "prompt.md").read_text(encoding="utf-8")), rel + "/prompt.md"))
        if (base / "case.yaml").is_file():
            texts.append(((base / "case.yaml").read_text(encoding="utf-8"), rel + "/case.yaml"))
        for text, where in texts:
            for grant in _grants(text, where):
                tool = grant.split("(", 1)[0].strip()
                if tool in FORBIDDEN_TOOLS or tool.startswith("mcp__") or not TOOL_NAME.match(tool):
                    raise Refused("%s grants %s; the evaluation CLI gives its runs the key through the environment, so "
                                  "only read-only tools may be granted" % (where, grant))
        if case_glob is None or fnmatch.fnmatchcase(base.name, case_glob):
            selected.append(base)
    return sorted(selected)


def set_runs(case_dir: Path, runs: int) -> None:
    """Sets the case's ``runs:`` to ``runs`` in ``prompt.md``'s frontmatter (which wins over ``case.yaml``), or in
    ``case.yaml`` when the case has no ``prompt.md``."""
    prompt, case = case_dir / "prompt.md", case_dir / "case.yaml"
    line = "runs: %d" % runs
    if prompt.is_file():
        text = prompt.read_text(encoding="utf-8")
        head = _frontmatter(text)
        if not head and not text.startswith("---\n"):
            text = "---\n%s\n---\n%s" % (line, text)
        elif re.search(r"(?m)^runs\s*:.*$", head):
            text = "---\n" + re.sub(r"(?m)^runs\s*:.*$", line, head, count=1) + text[4 + len(head):]
        else:
            text = "---\n%s\n%s" % (line, text[4:])
        prompt.write_text(text, encoding="utf-8")
    elif case.is_file():
        text = case.read_text(encoding="utf-8")
        if re.search(r"(?m)^runs\s*:.*$", text):
            text = re.sub(r"(?m)^runs\s*:.*$", line, text, count=1)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
        case.write_text(text, encoding="utf-8")


# -- the export -------------------------------------------------------------------------------------------------------

def _git_env(home: Path) -> Dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home)}


def export(rev: str, target: Path, home: Path) -> str:
    """Extracts ``git archive`` of ``rev`` into ``target``; returns the commit. ``SystemExit(2)`` for a bad revision."""
    if rev.startswith("-"):
        raise usage_error("--commit must name a revision")
    found = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                           env=_git_env(home), capture_output=True, text=True, timeout=60)
    if found.returncode != 0:
        raise usage_error("--commit %s is not a commit of %s" % (rev, REPO))
    sha = found.stdout.strip()
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", sha], env=_git_env(home),
                             capture_output=True, timeout=300)
    if archive.returncode != 0:
        raise usage_error("git archive failed for %s" % sha)
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        if hasattr(tarfile, "data_filter"):     # 3.12+, and the security releases of earlier versions
            tar.extractall(str(target), filter="data")
        else:
            tar.extractall(str(target))
    return sha


def stage(export_root: Path, skill: str, runs: int) -> Dict[str, Any]:
    """Moves the skill's cases to ``plugin-evals/<skill>/``, removes every ``skills/*/evals/``, sets each case's runs and
    writes the root manifest when there is none. ``SystemExit(2)`` when the skill has no plugin eval cases."""
    source = export_root / "skills" / skill / "evals" / "plugin"
    if not source.is_dir() or source.is_symlink():
        raise usage_error("skills/%s/evals/plugin is not in the export: no such skill, or it has no plugin eval cases" % skill)
    cases = export_root / CASES_ROOT / skill
    shutil.copytree(str(source), str(cases), symlinks=True, ignore=shutil.ignore_patterns("prompts.json", "results"))
    removed = sorted(export_root.glob("skills/*/evals"))
    for directory in removed:
        if directory.is_symlink() or not directory.is_dir():
            directory.unlink()
        else:
            shutil.rmtree(str(directory))
    manifest = export_root / ".claude-plugin" / "plugin.json"
    written = not manifest.exists()
    if written:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": PLUGIN_NAME}) + "\n", encoding="utf-8")
    return {"cases": cases, "eval_dir": "%s/%s" % (CASES_ROOT, skill), "evals_removed": len(removed),
            "manifest_written": written}


# -- the CLI ----------------------------------------------------------------------------------------------------------

def clean_env(home: Path, config_dir: Path, key: Optional[str] = None) -> Dict[str, str]:
    """The whole environment of a ``claude`` call: nothing is copied from the caller but ``PATH`` (and the key, when
    given)."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home), "CLAUDE_CONFIG_DIR": str(config_dir),
           "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if key is not None:
        env[KEY_VAR] = key
    return env


def missing_flags(exe: str, home: Path, config_dir: Path) -> List[str]:
    try:
        shown = subprocess.run([exe, "plugin", "eval", "--help"], env=clean_env(home, config_dir), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=120)
        text = shown.stdout + shown.stderr
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    return [flag for flag in ESSENTIAL_FLAGS if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(flag), text)]


def build_command(exe: str, export_root: Path, skill: str, args: argparse.Namespace, cap: str) -> List[str]:
    out = Path(args.out)
    command = [exe, "plugin", "eval", str(export_root), "--eval-dir", "%s/%s" % (CASES_ROOT, skill), "--ablation", "none",
               "--runs", str(args.runs), "--model", args.model, "--judge-model", args.judge_model, "--threshold", "0",
               "--no-publish", "--trust-plugin", "--json", str(out / "result.json"), "--output-dir", str(out / "results"),
               "--max-cost-usd", cap]
    if args.case:
        command += ["--case", args.case]
    return command


# -- the result -------------------------------------------------------------------------------------------------------

def _amount(value: Any) -> Optional[float]:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return float(value) if ok else None


def read_result(path: Path) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def verdicts(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per case: each run's grader results and the run's verdict, which is a pass only when the run has graders and
    every one passed. ``casesPassed`` and the CLI's own run ``passed`` are not used."""
    shown: List[Dict[str, Any]] = []
    for case in doc.get("cases") or []:
        if not isinstance(case, dict):
            continue
        runs = []
        for run in ((case.get("arms") or {}).get("with") or []):
            if not isinstance(run, dict):
                continue
            graders = [g for g in (run.get("graders") or []) if isinstance(g, dict)]
            runs.append({"passed": bool(graders) and all(g.get("passed") is True for g in graders),
                         "error": run.get("error"), "cost_usd": run.get("costUsd"), "judge_cost_usd": run.get("judgeCostUsd"),
                         "duration_seconds": run.get("durationSeconds"), "turns": run.get("turns"),
                         "skipped_paid_graders": run.get("skippedPaidGraders"), "graders": graders})
        shown.append({"name": case.get("name"), "runs_per_case": case.get("runsPerCase"), "runs": runs,
                      "passes": sum(1 for r in runs if r["passed"]), "fails": sum(1 for r in runs if not r["passed"])})
    return shown


def runs_differ(cases: Sequence[Dict[str, Any]], runs: int) -> List[str]:
    return [str(c["name"]) for c in cases if c["runs_per_case"] != runs or len(c["runs"]) > runs]


def redact_tree(root: Path, key: str) -> Dict[str, List[str]]:
    """Replaces the key's forms in every regular file under ``root``; symbolic links are listed, never followed."""
    redacted, links = [], []
    for base, dirs, files in os.walk(str(root), followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            if path.is_symlink():
                links.append(str(path.relative_to(root)))
                continue
            if name in files and path.is_file():
                data = path.read_bytes()
                clean = redact_bytes(data, key)
                if clean != data:
                    path.write_bytes(clean)
                    redacted.append(str(path.relative_to(root)))
    return {"redacted": sorted(redacted), "links": sorted(links)}


# -- main -------------------------------------------------------------------------------------------------------------

def parse(argv: Sequence[str]) -> argparse.Namespace:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:  # argparse exits 2 already; keep the message on stderr only
            print("error: " + message, file=sys.stderr)
            raise SystemExit(2)

    p = Parser(description="Run one skill's claude plugin eval cases on an exported copy, under the ledger's budget.")
    p.add_argument("--skill", required=True)
    p.add_argument("--case")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--model", required=True)
    p.add_argument("--judge-model", default=DEFAULT_JUDGE)
    p.add_argument("--budget-usd", required=True, type=float)
    p.add_argument("--ledger", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--commit", default="HEAD")
    args = p.parse_args(list(argv))
    if not SKILL_NAME.match(args.skill):
        raise usage_error("--skill must be a skill directory name")
    if args.case is not None and (not args.case or "/" in args.case or args.case.startswith("-")):
        raise usage_error("--case must be a case-name glob")
    if not 1 <= args.runs <= 50:
        raise usage_error("--runs must be between 1 and 50")
    if not (math.isfinite(args.budget_usd) and args.budget_usd > 0):
        raise usage_error("--budget-usd must be a positive number")
    for name in ("model", "judge_model"):
        if not getattr(args, name) or getattr(args, name).startswith("-"):
            raise usage_error("--%s must name a model" % name.replace("_", "-"))
    args.out = str(Path(args.out).resolve())
    return args


def _on_sigterm(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt()


def main(argv: Sequence[str]) -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)       # before anything is created, so a SIGTERM always reaches a ``finally``
    args = parse(argv)
    key = os.environ.get(KEY_VAR, "")
    if not key:
        return refuse("%s is not set. Evaluation runs are paid with an API key from that variable; set it yourself, the "
                      "wrapper never asks for, stores or creates one" % KEY_VAR)
    exe = shutil.which("claude")
    if exe is None:
        raise usage_error("the claude CLI is not on PATH")
    out = Path(args.out)
    if (out / "result.json").exists() or (out / "results").exists():
        raise usage_error("--out already holds a result; give each run a fresh directory")
    try:
        out.mkdir(parents=True, exist_ok=True)          # before the ledger's start line, which nothing may strand
    except OSError as exc:
        raise usage_error("--out cannot be created: %s" % exc)
    tmp = Path(tempfile.mkdtemp(prefix="qa-plugin-eval-"))
    try:
        for name in ("home", "config", "probe-home", "probe-config", "work"):
            (tmp / name).mkdir()
        export_root = tmp / PLUGIN_NAME
        sha = export(args.commit, export_root, tmp / "home")
        staged = stage(export_root, args.skill, args.runs)
        try:
            selected = check_cases(staged["cases"], args.case)
        except Refused as exc:
            return refuse(str(exc))
        if not selected:
            raise usage_error("no case of %s matches --case %s" % (args.skill, args.case))
        for case in check_cases(staged["cases"], None):
            set_runs(case, args.runs)
        missing = missing_flags(exe, tmp / "probe-home", tmp / "probe-config")
        if missing:
            return refuse("the installed claude CLI's plugin eval does not list %s; refusing to run without them"
                          % ", ".join(missing))
        return _run(args, key, exe, tmp, export_root, sha, staged, [c.name for c in selected], out)
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def _run(args: argparse.Namespace, key: str, exe: str, tmp: Path, export_root: Path, sha: str, staged: Dict[str, Any],
         cases: List[str], out: Path) -> int:
    ledger = Ledger(Path(args.ledger))
    run_id = uuid.uuid4().hex[:12]
    case_id = "plugin-eval:" + args.skill
    try:
        with ledger.locked():
            cap = run_cap(check_budget(ledger.read(), args.budget_usd))
            command = build_command(exe, export_root, args.skill, args, cap)
            ledger.append({"event": "start", "run_id": run_id, "timestamp": now_utc(), "case_id": case_id,
                           "config": "plugin-eval", "model": args.model})
    except Refused as exc:
        return refuse(str(exc))
    outcome = _harness.execute(command, "", tmp / "work", WALL_TIMEOUT, clean_env(tmp / "home", tmp / "config", key))
    pending = outcome.pop("pending", None)
    doc = None if outcome["interrupted"] else read_result(out / "result.json")
    cost = _amount(doc.get("costUsd")) if doc is not None else None
    duration = _amount(doc.get("durationSeconds")) if doc is not None else None
    shown = verdicts(doc) if doc is not None else []
    mismatched = runs_differ(shown, args.runs)
    errors = [r["error"] for c in shown for r in c["runs"] if r["error"]]
    if outcome["interrupted"]:
        status = "interrupted"
    elif outcome["timed_out"]:
        status = "timeout"
    elif doc is None or cost is None:
        status = "failed"
    elif mismatched:
        status = "runs_mismatch"
    elif doc.get("partial") is True:
        status = "partial"
    elif outcome["exit_code"] != 0 or errors:
        status = "failed"
    else:
        status = "ok"
    record = {"event": "end", "run_id": run_id, "timestamp": now_utc(), "case_id": case_id, "config": "plugin-eval",
              "model": args.model, "judge_model": args.judge_model, "engine": "claude plugin eval", "skill": args.skill,
              "case_glob": args.case, "cases": cases, "runs": args.runs, "commit": sha, "exit_code": outcome["exit_code"],
              "partial": doc.get("partial") if doc is not None else None,
              "partial_reason": doc.get("partialReason") if doc is not None else None,
              "cost_usd": cost, "cost_unknown": cost is None,
              "duration_ms": int(round(duration * 1000)) if duration is not None else None, "wall_ms": outcome["wall_ms"],
              "timed_out": outcome["timed_out"], "status": status, "runs_mismatch": mismatched, "run_errors": len(errors),
              "claude_version": doc.get("claudeVersion") if doc is not None else None,
              "swept_pids": len(outcome["swept"]), "sweeper_blind": outcome["sweeper_blind"],
              "staging": {"eval_dir": staged["eval_dir"], "evals_removed": staged["evals_removed"],
                          "manifest_written": staged["manifest_written"]},
              "command": [a.replace(str(tmp), "<tmp>") for a in command[1:]]}
    try:                                       # first the ledger line, before anything that can fail
        ledger.append(record)
    except OSError as exc:
        print("LEDGER WRITE FAILED (%s); the record follows so the spend is not lost:\n%s"
              % (exc, redact_bytes(json.dumps(record, sort_keys=True).encode("utf-8"), key).decode("utf-8")),
              file=sys.stderr)
        return 1
    problems: List[str] = []
    try:
        if (out / "costs.jsonl").resolve() != ledger.path.resolve():
            append_line(out / "costs.jsonl", record)
        (out / "cli-stdout.txt").write_bytes(redact_bytes(outcome["stdout"].encode("utf-8"), key))
        (out / "cli-stderr.txt").write_bytes(redact_bytes(outcome["stderr"].encode("utf-8"), key))
        (out / "summary.json").write_text(json.dumps({"run_id": run_id, "skill": args.skill, "status": status,
                                                      "cost_usd": cost, "cases": shown}, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    except OSError as exc:
        problems.append("evidence not fully saved: %s" % type(exc).__name__)
    try:                                       # every file under DIR, the CLI's included
        scan = redact_tree(out, key)
    except OSError as exc:
        scan = {"redacted": [], "links": []}
        problems.append("redaction scan failed: %s" % type(exc).__name__)
    if scan["links"]:
        problems.append("symbolic links under --out, not followed: %s" % ", ".join(scan["links"]))
    for problem in problems:
        print(problem + " (the ledger line was written)", file=sys.stderr)
    summary = {"run_id": run_id, "status": status, "cost_usd": cost, "cost_unknown": cost is None, "out": str(out),
               "redacted_files": scan["redacted"],
               "cases": [{"name": c["name"], "runs": len(c["runs"]), "passes": c["passes"], "fails": c["fails"]} for c in shown]}
    print(redact_bytes(json.dumps(summary, sort_keys=True).encode("utf-8"), key).decode("utf-8"))
    if pending is not None and not isinstance(pending, KeyboardInterrupt):
        raise pending
    return 0 if status == "ok" and not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
