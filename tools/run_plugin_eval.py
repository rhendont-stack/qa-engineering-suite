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
``prompt.md``'s frontmatter or in ``case.yaml``. A ``prompt.md`` whose frontmatter it cannot delimit, one with an
``allowed_tools`` line outside its frontmatter and a case file that is not UTF-8 count as grants it cannot read. The CLI
passes the key to its children through the environment, so a case must not be able to run or send anything.

Every ``claude`` call gets an environment built from scratch: ``PATH``, a temporary ``HOME`` and ``CLAUDE_CONFIG_DIR``,
``DISABLE_TELEMETRY=1`` and ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``, plus ``ANTHROPIC_API_KEY`` from the caller's
environment for the paid call only (the ``--help`` probe gets no key). Nothing else is copied from the caller.

The budget authority is the ledger (``--ledger``), shared with ``tools/run_skill_eval.py`` and read under its exclusive
lock: a ``start`` line before the run and an ``end`` line the moment it finishes, before any evidence is written. A
``start`` with no ``end``, or an ``end`` with an unknown cost, refuses every later run. The run's cap (``--max-cost-usd``)
is the budget left over the whole ledger. The cost is the result's ``costUsd`` (judge calls included); a missing or
unreadable ``costUsd``, a missing result and an interrupted run are recorded as unknown, never as 0.

Verdicts come from each run's grader results, never from the result's ``casesPassed``, which counts every case scoring at
least ``--threshold`` (0 here). A run is ``pass`` when it has graders and every one passed, ``skipped`` when the CLI
skipped its paid graders (a skipped grader is not a passed one), and ``fail`` otherwise; the three are counted apart. A
result value of an unexpected JSON type is recorded in the ``end`` line's ``shape_problems`` and makes the status
``failed``. A run the CLI reports as stopped at its case's own ``max_turns`` is graded as usual and counted in the
``end`` line's ``max_turns_stops``; any other run error makes the status ``failed``. The ``end`` line is redacted before
it is appended, because it copies text from the CLI's result.

``DIR`` must resolve, symbolic links included, under the repository's ``.superpowers/`` and never under ``skills/``, and
must not already hold a result. It receives ``result.json`` and ``results/`` (the CLI's), ``cli-stdout.txt``,
``cli-stderr.txt``, ``summary.json`` (per case: runs, passes, fails, skipped, and each run's verdict and grader results)
and ``costs.jsonl`` (the ``end`` line again). Once the CLI has returned, every file under ``DIR`` is scanned on every
path, the ledger failure and any exception included, and the key's forms (``redact_bytes``) are replaced by the marker.
Symbolic links are not followed. A directory the scan cannot list and a file it cannot read, redact or write back are
named on stderr and in the printed summary's ``unscanned``, and the scan goes on past them.

Exit codes: 0 the run finished and was recorded with status ``ok``; 1 it was recorded with another status (``failed``,
``partial``, ``runs_mismatch``, ``timeout``, ``interrupted``), its evidence was not fully saved, or a path under ``DIR``
was left unscanned; 2 bad arguments; 3 refused (a forbidden or unreadable grant, a CLI without an essential flag, no
key, the budget reached or an unknown recorded cost).
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
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


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
redact = _harness.redact
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
# Line breaks other than LF that ``str.splitlines`` honours (YAML's CR among them): a frontmatter holding one is refused.
OTHER_LINE_BREAKS = "\r\v\f\x1c\x1d\x1e\x85  "
# The run error the CLI reports when a run stops at its case's own ``max_turns``: the run is still graded as usual.
TURN_LIMIT = re.compile(r"\A(?:exit \d+: )?Reached maximum number of turns \(\d+\)\Z")


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


def _prompt_head(text: str, where: str) -> str:
    """A ``prompt.md``'s frontmatter, for the grant check: the lines between an opening ``---`` line and the first later
    line that starts with ``---``, which must be exactly ``---`` (where ``_frontmatter`` ends it too). Raises ``Refused``
    for a file that opens with anything else delimiter-like (other line endings, a byte-order mark or blank lines first),
    for a frontmatter that is never closed, and for an ``allowed_tools`` line outside the frontmatter: a grant the check
    cannot place cannot be shown to be safe."""
    lines = text.split("\n")
    head: List[str] = []
    rest = lines
    if text.startswith("---\n"):
        for number in range(1, len(lines)):
            if lines[number].startswith("---"):
                if lines[number] != "---":
                    raise Refused("%s: the frontmatter's closing line is not a plain ---; refusing a grant that cannot "
                                  "be read" % where)
                head, rest = lines[1:number], lines[number + 1:]
                break
        else:
            raise Refused("%s: the frontmatter is never closed; refusing a grant that cannot be read" % where)
        if any(mark in line for line in head for mark in OTHER_LINE_BREAKS):
            raise Refused("%s: the frontmatter holds a line break other than LF; refusing a grant that cannot be read"
                          % where)
    elif text.lstrip("﻿ \t\r\n").startswith("---"):
        raise Refused("%s: the file opens with a frontmatter delimiter the check cannot read; refusing a grant that "
                      "cannot be read" % where)
    if any(GRANT_LINE.match(line) for line in "\n".join(rest).splitlines()):
        raise Refused("%s: an allowed_tools line outside the frontmatter; refusing a grant that cannot be read" % where)
    return "\n".join(head) + "\n"


def _case_text(path: Path, where: str) -> str:
    """The file's text exactly as the CLI reads it: no newline translation, so other line endings stay visible."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, ValueError) as exc:
        raise Refused("%s cannot be read as UTF-8 text (%s); refusing a grant that cannot be read"
                      % (where, type(exc).__name__))


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
            where = rel + "/prompt.md"
            texts.append((_prompt_head(_case_text(base / "prompt.md", where), where), where))
        if (base / "case.yaml").is_file():
            texts.append((_case_text(base / "case.yaml", rel + "/case.yaml"), rel + "/case.yaml"))
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


def stage(export_root: Path, skill: str) -> Dict[str, Any]:
    """Copies the skill's cases to ``plugin-evals/<skill>/``, removes every ``skills/*/evals/`` and writes the root manifest
    when there is none. ``SystemExit(2)`` when the skill has no plugin eval cases."""
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


def verdicts(doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Per case: each run's grader results and verdict, and the result's shape problems. A run is ``skipped`` when the
    CLI skipped its paid graders, ``pass`` when it has graders and every one passed, and ``fail`` otherwise.
    ``casesPassed`` and the CLI's own run ``passed`` are not used. A ``cases``, ``arms``, ``with``, run, ``graders`` or
    ``skippedPaidGraders`` value of an unexpected JSON type is left out and named in the shape problems, which the run
    records as a failure; this never raises on a parsed JSON document."""
    shown: List[Dict[str, Any]] = []
    odd: List[str] = []
    cases = doc.get("cases")
    if not isinstance(cases, list):
        odd.append("cases is not a list")
        cases = []
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            odd.append("cases[%d] is not an object" % i)
            continue
        arms = case.get("arms")
        made = arms.get("with") if isinstance(arms, dict) else None
        if not isinstance(made, list):
            odd.append("cases[%d].arms.with is not a list" % i)
            made = []
        runs = []
        for j, run in enumerate(made):
            where = "cases[%d].arms.with[%d]" % (i, j)
            if not isinstance(run, dict):
                odd.append(where + " is not an object")
                continue
            listed = run.get("graders")
            if not isinstance(listed, list):
                odd.append(where + ".graders is not a list")
                listed = []
            graders = [g for g in listed if isinstance(g, dict)]
            if len(graders) != len(listed):
                odd.append(where + ".graders holds a value that is not an object")
            skipped = run.get("skippedPaidGraders")
            if not (skipped is None or isinstance(skipped, bool)):
                odd.append(where + ".skippedPaidGraders is not a boolean")
            if skipped is True:
                verdict = "skipped"                 # a skipped grader is not a passed grader
            elif graders and all(g.get("passed") is True for g in graders):
                verdict = "pass"
            else:
                verdict = "fail"
            runs.append({"verdict": verdict, "passed": verdict == "pass", "error": run.get("error"),
                         "cost_usd": run.get("costUsd"), "judge_cost_usd": run.get("judgeCostUsd"),
                         "duration_seconds": run.get("durationSeconds"), "turns": run.get("turns"),
                         "skipped_paid_graders": skipped, "graders": graders})
        shown.append({"name": case.get("name"), "runs_per_case": case.get("runsPerCase"), "runs": runs,
                      "passes": sum(1 for r in runs if r["verdict"] == "pass"),
                      "fails": sum(1 for r in runs if r["verdict"] == "fail"),
                      "skipped": sum(1 for r in runs if r["verdict"] == "skipped")})
    return shown, odd


def runs_differ(cases: Sequence[Dict[str, Any]], runs: int) -> List[str]:
    return [str(c["name"]) for c in cases if c["runs_per_case"] != runs or len(c["runs"]) > runs]


def redact_value(value: Any, key: str) -> Any:
    """``value`` with the key's forms replaced in every string it holds, keys of objects included."""
    if isinstance(value, str):
        return redact(value, key)
    if isinstance(value, list):
        return [redact_value(item, key) for item in value]
    if isinstance(value, dict):
        return {redact_value(k, key): redact_value(v, key) for k, v in value.items()}
    return value


def redact_tree(root: Path, key: str) -> Dict[str, List[str]]:
    """Replaces the key's forms in every regular file under ``root``. Symbolic links are listed, never followed. A
    directory that cannot be listed, a path that is not a regular file, and a file that cannot be read, redacted or
    written back are listed in ``unscanned`` with the reason, and the scan goes on past them. It never raises an
    ``Exception``."""
    redacted: List[str] = []
    links: List[str] = []
    unscanned: List[str] = []

    def rel(path: Any) -> str:
        try:
            return str(Path(path).relative_to(root))
        except (TypeError, ValueError):
            return str(path)

    def cannot_list(exc: OSError) -> None:
        unscanned.append("%s (cannot be listed: %s)" % (rel(exc.filename if exc.filename else root), type(exc).__name__))

    try:
        for base, dirs, files in os.walk(str(root), onerror=cannot_list, followlinks=False):
            for name in dirs + files:
                path = Path(base) / name
                try:
                    if path.is_symlink():
                        links.append(rel(path))
                    elif name not in files:
                        continue                    # a directory: the walk enters it
                    elif not path.is_file():
                        unscanned.append("%s (not a regular file)" % rel(path))
                    else:
                        data = path.read_bytes()
                        clean = redact_bytes(data, key)
                        if clean != data:
                            path.write_bytes(clean)
                            redacted.append(rel(path))
                except Exception as exc:            # one file's failure must not end the scan for the others
                    unscanned.append("%s (%s)" % (rel(path), type(exc).__name__))
    except Exception as exc:
        unscanned.append("%s (the scan stopped: %s)" % (rel(root), type(exc).__name__))
    return {"redacted": sorted(redacted), "links": sorted(links), "unscanned": sorted(unscanned)}


def scan_out(out: Path, key: str) -> Tuple[Dict[str, List[str]], List[int]]:
    """``redact_tree`` of ``out`` with SIGTERM and SIGINT held until it ends, so a termination signal cannot cut the scan
    short. Returns the scan and the signals held, which the caller delivers again once it has reported the scan."""
    held: List[int] = []

    def hold(signum: int, _frame: Any) -> None:
        held.append(signum)

    previous = {number: signal.signal(number, hold) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        return redact_tree(out, key), sorted(set(held))
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


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
    out = Path(args.out).resolve()                  # symbolic links resolved, so a link cannot lead the output elsewhere
    allowed, skills = (REPO / ".superpowers").resolve(), (REPO / "skills").resolve()
    if not _inside(out, allowed) or _inside(out, skills, or_equal=True):
        raise usage_error("--out must resolve to a directory under %s, never under %s" % (allowed, skills))
    args.out = str(out)
    return args


def _inside(path: Path, parent: Path, or_equal: bool = False) -> bool:
    """Whether ``path`` is below ``parent`` (or is it, with ``or_equal``), comparing NFC forms: macOS keeps either
    Unicode form in a path, and the two resolved paths may come from different sources."""
    child, base = unicodedata.normalize("NFC", str(path)), unicodedata.normalize("NFC", str(parent))
    return (or_equal and child == base) or child.startswith(base.rstrip(os.sep) + os.sep)


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
        staged = stage(export_root, args.skill)
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
    try:                                       # from here on, the scan in ``finally`` runs on every path
        written = _record(args, key, tmp, sha, staged, cases, out, ledger, run_id, case_id, command, outcome)
    finally:                                   # every file under DIR, the CLI's included
        scan, held = scan_out(out, key)
        for path in scan["unscanned"]:
            print(redact("NOT SCANNED FOR THE KEY, check it before sharing anything under --out: %s" % path, key),
                  file=sys.stderr)
        for number in held:                    # a termination signal that arrived during the scan, delivered now
            signal.raise_signal(number)
    if written is None:                        # the ledger line was not written; its record is on stderr
        return 1
    status, shown, cost, problems = written
    if scan["unscanned"]:
        problems.append("%d path(s) under --out not scanned for the key" % len(scan["unscanned"]))
    if scan["links"]:
        problems.append("symbolic links under --out, not followed: %s" % ", ".join(scan["links"]))
    for problem in problems:
        print(redact(problem, key) + " (the ledger line was written)", file=sys.stderr)
    summary = {"run_id": run_id, "status": status, "cost_usd": cost, "cost_unknown": cost is None, "out": str(out),
               "redacted_files": scan["redacted"], "unscanned": scan["unscanned"],
               "cases": [{"name": c["name"], "runs": len(c["runs"]), "passes": c["passes"], "fails": c["fails"],
                          "skipped": c["skipped"]} for c in shown]}
    print(redact_bytes(json.dumps(summary, sort_keys=True).encode("utf-8"), key).decode("utf-8"))
    if pending is not None and not isinstance(pending, KeyboardInterrupt):
        raise pending
    return 0 if status == "ok" and not problems else 1


def _record(args: argparse.Namespace, key: str, tmp: Path, sha: str, staged: Dict[str, Any], cases: List[str], out: Path,
            ledger: Any, run_id: str, case_id: str, command: List[str],
            outcome: Dict[str, Any]) -> Optional[Tuple[str, List[Dict[str, Any]], Optional[float], List[str]]]:
    """Writes the run's ``end`` line, then its evidence. Returns the status, the cases, the cost and the evidence
    problems, or None when the ``end`` line could not be written (its redacted record is then printed on stderr)."""
    doc = None if outcome["interrupted"] else read_result(out / "result.json")
    cost = _amount(doc.get("costUsd")) if doc is not None else None
    duration = _amount(doc.get("durationSeconds")) if doc is not None else None
    shown, odd = verdicts(doc) if doc is not None else ([], [])
    mismatched = runs_differ(shown, args.runs)
    stops = [r["error"] for c in shown for r in c["runs"] if r["error"] and TURN_LIMIT.search(str(r["error"]))]
    errors = [r["error"] for c in shown for r in c["runs"] if r["error"] and not TURN_LIMIT.search(str(r["error"]))]
    if outcome["interrupted"]:
        status = "interrupted"
    elif outcome["timed_out"]:
        status = "timeout"
    elif doc is None or cost is None or odd:
        status = "failed"
    elif mismatched:
        status = "runs_mismatch"
    elif doc.get("partial") is True:
        status = "partial"
    elif outcome["exit_code"] != 0 or errors:
        status = "failed"
    else:
        status = "ok"
    counts = {verdict: sum(1 for c in shown for r in c["runs"] if r["verdict"] == verdict)
              for verdict in ("pass", "fail", "skipped")}
    record = {"event": "end", "run_id": run_id, "timestamp": now_utc(), "case_id": case_id, "config": "plugin-eval",
              "model": args.model, "judge_model": args.judge_model, "engine": "claude plugin eval", "skill": args.skill,
              "case_glob": args.case, "cases": cases, "runs": args.runs, "commit": sha, "exit_code": outcome["exit_code"],
              "partial": doc.get("partial") if doc is not None else None,
              "partial_reason": doc.get("partialReason") if doc is not None else None,
              "cost_usd": cost, "cost_unknown": cost is None,
              "duration_ms": int(round(duration * 1000)) if duration is not None else None, "wall_ms": outcome["wall_ms"],
              "timed_out": outcome["timed_out"], "status": status, "verdicts": counts, "shape_problems": odd,
              "runs_mismatch": mismatched, "run_errors": len(errors), "max_turns_stops": len(stops),
              "claude_version": doc.get("claudeVersion") if doc is not None else None,
              "swept_pids": len(outcome["swept"]), "sweeper_blind": outcome["sweeper_blind"],
              "staging": {"eval_dir": staged["eval_dir"], "evals_removed": staged["evals_removed"],
                          "manifest_written": staged["manifest_written"]},
              "command": [a.replace(str(tmp), "<tmp>") for a in command[1:]]}
    record = redact_value(record, key)         # it copies text from the CLI's result, and the ledger is outside DIR
    try:                                       # first the ledger line, before anything that can fail
        ledger.append(record)
    except OSError as exc:
        print("LEDGER WRITE FAILED (%s); the record follows so the spend is not lost:\n%s"
              % (redact(str(exc), key), json.dumps(record, sort_keys=True)), file=sys.stderr)
        return None
    problems: List[str] = []
    try:
        if (out / "costs.jsonl").resolve() != ledger.path.resolve():
            append_line(out / "costs.jsonl", record)
        (out / "cli-stdout.txt").write_bytes(redact_bytes(outcome["stdout"].encode("utf-8"), key))
        (out / "cli-stderr.txt").write_bytes(redact_bytes(outcome["stderr"].encode("utf-8"), key))
        (out / "summary.json").write_text(json.dumps({"run_id": run_id, "skill": args.skill, "status": status,
                                                      "cost_usd": cost, "verdicts": counts, "cases": shown},
                                                     indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        problems.append("evidence not fully saved: %s" % type(exc).__name__)
    return status, shown, cost, problems

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
