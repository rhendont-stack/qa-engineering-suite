"""The benchmark's judge: one ``claude -p --output-format json`` call, with no tools, that grades a run's saved material.

``run_judge(rubric, material, *, model, budget_usd, ledger, out, subject="", stage_remaining_usd=None, timeout=…)``:

- **Refusals.** ``JudgeRefused`` (nothing called, no ledger line) when ``ANTHROPIC_API_KEY`` is not set, ``claude`` is not
  on ``PATH``, the installed CLI's ``--help`` does not list every flag in ``REQUIRED_FLAGS``, or the ledger refuses
  (``check_budget``: a run in flight, an unknown cost, the budget spent; ``run_cap``: too little left).
- **Isolation**, the same as the harness's agent: the environment is ``child_env`` (``PATH``, a ``HOME`` and a
  ``CLAUDE_CONFIG_DIR`` made for this call, ``DISABLE_TELEMETRY=1``, ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``; nothing
  inherited, so no key); the key reaches the CLI only through an ``apiKeyHelper`` (``write_key_helper``) named in a
  ``--settings`` file that holds nothing else. ``--bare`` skips hooks, plugins, keychain reads and CLAUDE.md discovery, and
  takes the key only from ``ANTHROPIC_API_KEY`` (absent here) or that helper; ``--tools ""`` gives no tools at all;
  ``--disable-slash-commands``, ``--strict-mcp-config`` with an empty ``--mcp-config``, ``--setting-sources project`` (the
  working directory is the empty home) and ``--no-session-persistence`` leave nothing else. The home, the configuration
  and the helper are removed when the call is done, whichever way it ends.
- **Spend.** Under the ledger's lock, as the harness does: the cap is the smallest of the ledger's remainder (``budget_usd``
  less what the ledger records), ``stage_remaining_usd`` when given, and ``budget_usd``, rounded down (``run_cap``), passed
  as ``--max-budget-usd``; then the ``start`` line (``engine: "judge"``, ``config: "judge"``, ``case_id: "judge:<subject>"``).
  After the call the ``end`` line goes to the ledger and then to ``out/costs.jsonl`` (unless that is the ledger), so the
  stage's spend counts it. A cost the CLI did not report is ``cost_unknown: true`` and ``cost_usd: null``, never 0.
- **Answer.** The prompt (the rubric, then the material as JSON between ``<material>`` tags) goes through stdin. The prompt,
  the raw stdout and the stderr are saved, redacted, as ``out/grades/<subject>.judge-prompt.txt``, ``.judge-raw.txt`` and
  ``.judge-stderr.txt``. The answer is the result text read as one JSON object, or as the one fenced ``json`` block it holds;
  anything else is ``{"error": "judge_unparseable"}``, never a guess, and a call that did not finish ok is
  ``{"error": "judge_failed"}``.

Returns ``{"answer", "model", "cost_usd", "cost_unknown", "raw_path" (relative to out), "judge_id", "status",
"costs_copied"}``. An interrupt during the call is raised again once its end line is written.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tools" / "run_skill_eval.py"
REQUIRED_FLAGS = ("-p", "--output-format", "--model", "--bare", "--tools", "--disable-slash-commands", "--setting-sources",
                  "--strict-mcp-config", "--mcp-config", "--settings", "--no-session-persistence", "--system-prompt",
                  "--max-budget-usd")
SYSTEM_PROMPT = ("You are a grader. You have no tools. Read the rubric and the material in the user message, and answer "
                 "with exactly one JSON object as the rubric defines it, and nothing else.")
UNPARSEABLE = {"error": "judge_unparseable"}
FAILED = {"error": "judge_failed"}
DEFAULT_TIMEOUT = 600
FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("qa_run_skill_eval_for_judge", str(HARNESS))
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_harness = _load_harness()
Ledger = _harness.Ledger
KEY_VAR = _harness.KEY_VAR


class JudgeRefused(Exception):
    """The judge was not called: nothing was spent and no ledger line was written."""


def compose(rubric: str, material: Mapping[str, Any]) -> str:
    return "%s\n\n<material>\n%s\n</material>\n" % (rubric.rstrip(), json.dumps(material, indent=1, sort_keys=True,
                                                                                ensure_ascii=False))


def parse_answer(text: Any) -> Dict[str, Any]:
    """The one JSON object ``text`` is, or the one fenced block it holds; ``UNPARSEABLE`` otherwise."""
    if not isinstance(text, str):
        return dict(UNPARSEABLE)
    candidates = [text.strip()] if text.strip().startswith("{") else []
    if not candidates:
        blocks = FENCE.findall(text)
        candidates = blocks if len(blocks) == 1 else []
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return dict(UNPARSEABLE)


def _result_event(stdout: str) -> Dict[str, Any]:
    """The ``result`` object of ``--output-format json`` output: the whole stdout, or its last line that is one."""
    try:
        whole = json.loads(stdout)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return whole
    events = _harness.parse_events(stdout)
    return next((e for e in reversed(events) if e.get("type") == "result"), {})


def run_judge(rubric: str, material: Mapping[str, Any], *, model: str, budget_usd: float, ledger: Any, out: Path,
              subject: str = "", stage_remaining_usd: Optional[float] = None, timeout: int = DEFAULT_TIMEOUT,
              exe: Optional[str] = None) -> Dict[str, Any]:
    key = os.environ.get(KEY_VAR, "")
    if not key:
        raise JudgeRefused("%s is not set; the judge is paid with an API key from that variable and never asks for one"
                           % KEY_VAR)
    exe = exe or shutil.which("claude")
    if exe is None:
        raise JudgeRefused("the claude CLI is not on PATH")
    help_text = _harness.cli_help(exe)
    missing = [flag for flag in REQUIRED_FLAGS if flag not in help_text]
    if missing:
        raise JudgeRefused("the installed claude CLI does not list %s; the judge does not run without them" % ", ".join(missing))
    book = ledger if isinstance(ledger, Ledger) else Ledger(Path(ledger))
    out = Path(out)
    judge_id = uuid.uuid4().hex[:12]
    name = subject or judge_id
    prompt = compose(rubric, material)
    root = Path(tempfile.mkdtemp(prefix="qa-judge-home-"))
    helper_dir = Path(tempfile.mkdtemp(prefix="qa-judge-key-"))
    try:
        home, config = root / "home", root / "config"
        home.mkdir()
        config.mkdir()
        mcp, settings = config / "mcp.json", config / "settings.json"
        mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        try:
            settings.write_text(json.dumps({"apiKeyHelper": _harness.write_key_helper(key, helper_dir)}), encoding="utf-8")
            with book.locked():
                remaining = min(_harness.check_budget(book.read(), budget_usd), budget_usd)
                if stage_remaining_usd is not None:
                    remaining = min(remaining, stage_remaining_usd)
                cap = _harness.run_cap(remaining)
                argv = [exe, "-p", "--output-format", "json", "--model", model, "--bare", "--tools", "",
                        "--disable-slash-commands", "--setting-sources", "project", "--strict-mcp-config", "--mcp-config",
                        str(mcp), "--settings", str(settings), "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT,
                        "--max-budget-usd", cap]
                book.append({"event": "start", "run_id": judge_id, "timestamp": _harness.now_utc(),
                             "case_id": "judge:%s" % subject, "config": "judge", "model": model, "engine": "judge"})
        except _harness.Refused as exc:
            raise JudgeRefused(str(exc))
        outcome = _harness.execute(argv, prompt, home, timeout, _harness.child_env(home, config))
    finally:
        shutil.rmtree(str(root), ignore_errors=True)
        shutil.rmtree(str(helper_dir), ignore_errors=True)
    pending = outcome.pop("pending", None)
    stdout, stderr = _harness.redact(outcome["stdout"], key), _harness.redact(outcome["stderr"], key)
    event = _result_event(stdout)
    metrics = _harness.extract_metrics(json.dumps(event)) if event else _harness.extract_metrics("")
    cost = None if outcome["interrupted"] else metrics["cost_usd"]
    if outcome["interrupted"]:
        status = "interrupted"
    elif outcome["timed_out"]:
        status = "timeout"
    elif outcome["exit_code"] != 0 or metrics["is_error"] or not event:
        status = "failed"
    else:
        status = "ok"
    end = {"event": "end", "run_id": judge_id, "timestamp": _harness.now_utc(), "case_id": "judge:%s" % subject,
           "config": "judge", "model": model, "engine": "judge", "subject": subject, "command": argv[1:],
           "prompt_via": "stdin", "status": status, "exit_code": outcome["exit_code"], "timed_out": outcome["timed_out"],
           "cost_usd": cost, "cost_unknown": cost is None, "duration_ms": metrics["duration_ms"],
           "wall_ms": outcome.get("wall_ms"), "tokens": metrics["tokens"], "max_budget_usd": cap,
           "stage_remaining_usd": stage_remaining_usd}
    try:                                      # first the ledger line, before anything that can fail
        book.append(end)
    except OSError:
        print("LEDGER WRITE FAILED; the judge's end line follows so the spend is not lost:\n%s"
              % json.dumps(end, sort_keys=True), file=sys.stderr)
        raise
    copied = True
    try:
        if (out / "costs.jsonl").resolve() != book.path.resolve():
            _harness.append_line(out / "costs.jsonl", end)
    except OSError:
        copied = False
    grades = out / "grades"                   # the call's files, once its end line is written: a refusal leaves none
    grades.mkdir(parents=True, exist_ok=True)
    (grades / ("%s.judge-prompt.txt" % name)).write_text(_harness.redact(prompt, key), encoding="utf-8")
    raw = grades / ("%s.judge-raw.txt" % name)
    raw.write_text(stdout, encoding="utf-8")
    (grades / ("%s.judge-stderr.txt" % name)).write_text(stderr, encoding="utf-8")
    if isinstance(pending, BaseException):
        raise pending
    answer = parse_answer(event.get("result")) if status == "ok" else dict(FAILED)
    return {"answer": answer, "model": model, "cost_usd": cost, "cost_unknown": cost is None,
            "raw_path": str(raw.relative_to(out)), "judge_id": judge_id, "status": status, "costs_copied": copied}
