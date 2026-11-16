"""What a run's saved transcript shows: ``DIR/runs/<run_id>/stdout.txt``, the CLI's ``stream-json`` output, redacted.

- ``tool_calls(stdout)``: every tool call of the run, in order, the subagents' included: ``{"id", "name", "input",
  "subagent", "denied", "result", "is_error", "interrupted", "executed"}``. ``result`` is the text the tool result carried,
  or None when no result came back. A call is **denied** when the CLI refused it: a ``permission_denied`` event names it, the ``result``
  event's ``permission_denials`` list it, or its result is an error that starts with ``DENIED_RESULT``. A call is
  **executed** when a result came back and it was not denied.
- ``commands(stdout)``: each Bash call, ``{"id", "command", "executed", "exit_code", "denied", "output", "subagent"}``.
  ``exit_code`` is the ``N`` of a result that starts ``Exit code N`` (how the CLI reports a non-zero exit), 0 for a result
  that is not an error and was not interrupted, and None otherwise (no result, denied, or an error without a code).
- ``final_message(stdout)``: the ``result`` text of the last ``result`` event (with background tasks the CLI emits one per
  turn, the last one is the answer the user got), or ``""`` when there is none.
- ``observations(stdout, end, suite_skills)``: counts that are recorded and never graded (Task 10's report reads them):

  - ``bash``: Bash calls, denied ones, and executed ones whose failing result says a program or file was not found
    (``NOT_FOUND``).
  - ``scripts``: the Bash calls that name a skill's script (``skills/<skill>/scripts/<name>.py``, the first such word of
    the command), split into ``launcher`` (``qa.py``) and ``entry_script`` (any other), each by the form of that path:
    ``absolute`` under the staged plugin's directory as the run was given it (the end line's ``plugin.dir``),
    ``resolved`` under its resolved path (``plugin.resolved``, checked second, so where both are the same path a call
    counts as ``absolute``), and ``other`` (relative, or anything else); each form counts its calls and its denied calls.
  - ``skills``: Skill calls of the suite (``qa-engineering-suite:<skill>``, or a bare name in ``suite_skills``), other
    Skill calls, and denied Skill calls of either kind.
  - ``workload_model``: Read calls of ``skills/qa-performance/references/workload-model.md`` in any path form (its
    normalised path ends with that), and Bash calls whose command names it.
  - ``tools_beyond_allow``: as the end line records it.

Standard library only; no files, no state.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

PLUGIN_NAME = "qa-engineering-suite"
# How the CLI words the tool result of a call it refused (measured on milestone 4 transcripts).
DENIED_RESULT = "Permission for this tool use was denied"
EXIT_CODE = re.compile(r"\AExit code (-?\d+)\b")
NOT_FOUND = re.compile(r"(?i)\bcommand not found\b|\bnot found\b|\bno such file or directory\b")
SCRIPT = re.compile(r"(?:\A|/)skills/[a-z0-9][a-z0-9-]*/scripts/[A-Za-z0-9][A-Za-z0-9_.-]*\.py\Z")
WORKLOAD_MODEL = "skills/qa-performance/references/workload-model.md"
FORMS = ("absolute", "resolved", "other")


def events(stdout: str) -> List[Dict[str, Any]]:
    """The JSON objects of the transcript, one per line; a line that is not an object is skipped."""
    found: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def _blocks(event: Mapping[str, Any]) -> List[Dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")
    return content if isinstance(content, str) else ""


def _denied_ids(evs: List[Dict[str, Any]]) -> Set[str]:
    ids = {e.get("tool_use_id") for e in evs if e.get("type") == "system" and e.get("subtype") == "permission_denied"}
    for event in evs:
        if event.get("type") == "result" and isinstance(event.get("permission_denials"), list):
            ids |= {d.get("tool_use_id") for d in event["permission_denials"] if isinstance(d, dict)}
        if event.get("type") == "user":
            for block in _blocks(event):
                if block.get("type") == "tool_result" and block.get("is_error") is True \
                        and _text(block.get("content")).startswith(DENIED_RESULT):
                    ids.add(block.get("tool_use_id"))
    return {i for i in ids if isinstance(i, str)}


def tool_calls(stdout: str) -> List[Dict[str, Any]]:
    evs = events(stdout)
    denied = _denied_ids(evs)
    results: Dict[str, Dict[str, Any]] = {}
    for event in evs:
        if event.get("type") != "user":
            continue
        interrupted = isinstance(event.get("tool_use_result"), dict) and event["tool_use_result"].get("interrupted") is True
        for block in _blocks(event):
            if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                results.setdefault(block["tool_use_id"], {"text": _text(block.get("content")),
                                                          "is_error": block.get("is_error") is True,
                                                          "interrupted": interrupted})
    calls: List[Dict[str, Any]] = []
    for event in evs:
        if event.get("type") != "assistant":
            continue
        for block in _blocks(event):
            if block.get("type") != "tool_use":
                continue
            call_id = block.get("id") if isinstance(block.get("id"), str) else None
            result = results.get(call_id) if call_id else None
            refused = call_id in denied
            calls.append({"id": call_id, "name": block.get("name"),
                          "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                          "subagent": event.get("parent_tool_use_id") is not None, "denied": refused,
                          "result": None if result is None else result["text"],
                          "is_error": None if result is None else result["is_error"],
                          "interrupted": bool(result and result["interrupted"]),
                          "executed": result is not None and not refused})
    return calls


def _exit_code(call: Mapping[str, Any]) -> Optional[int]:
    if not call["executed"]:
        return None
    found = EXIT_CODE.match(call["result"] or "")
    if found:
        return int(found.group(1))
    return 0 if call["is_error"] is False and not call["interrupted"] else None


def commands(stdout: str) -> List[Dict[str, Any]]:
    return [{"id": c["id"], "command": str(c["input"].get("command", "")), "executed": c["executed"],
             "exit_code": _exit_code(c), "denied": c["denied"], "output": c["result"], "subagent": c["subagent"]}
            for c in tool_calls(stdout) if c["name"] == "Bash"]


def final_message(stdout: str) -> str:
    result = next((e for e in reversed(events(stdout)) if e.get("type") == "result"), {})
    text = result.get("result")
    return text if isinstance(text, str) else ""


def _words(command: str) -> List[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def script_path(command: str) -> Optional[str]:
    """The first word of ``command`` that names a skill's script (``skills/<skill>/scripts/<name>.py``), or None."""
    return next((word for word in _words(command) if SCRIPT.search(word)), None)


def path_form(path: str, plugin: Mapping[str, Any]) -> str:
    """``absolute`` (under the staged plugin's directory as given), ``resolved`` (under its resolved path) or ``other``."""
    for form, root in (("absolute", plugin.get("dir")), ("resolved", plugin.get("resolved"))):
        if isinstance(root, str) and root and path.startswith(root.rstrip("/") + "/"):
            return form
    return "other"


def observations(stdout: str, end: Mapping[str, Any], suite_skills: Iterable[str]) -> Dict[str, Any]:
    calls = tool_calls(stdout)
    suite = set(suite_skills)
    plugin = end.get("plugin") if isinstance(end.get("plugin"), dict) else {}
    bash = [c for c in calls if c["name"] == "Bash"]
    scripts = {kind: {form: {"calls": 0, "denied": 0} for form in FORMS} for kind in ("launcher", "entry_script")}
    for call in bash:
        path = script_path(str(call["input"].get("command", "")))
        if path is None:
            continue
        kind = "launcher" if posixpath.basename(path) == "qa.py" else "entry_script"
        counts = scripts[kind][path_form(path, plugin)]
        counts["calls"] += 1
        counts["denied"] += 1 if call["denied"] else 0
    skill_calls = [c for c in calls if c["name"] == "Skill"]

    def in_suite(call: Mapping[str, Any]) -> bool:
        name = str(call["input"].get("skill", ""))
        return name.startswith(PLUGIN_NAME + ":") or name in suite

    def names_model(path: str) -> bool:
        path = posixpath.normpath(path)
        return path == WORKLOAD_MODEL or path.endswith("/" + WORKLOAD_MODEL)

    reads = [c for c in calls if c["name"] == "Read" and names_model(str(c["input"].get("file_path", "")))]
    return {
        "bash": {"calls": len(bash), "denied": sum(1 for c in bash if c["denied"]),
                 "not_found": sum(1 for c in bash if c["executed"] and c["is_error"] and NOT_FOUND.search(c["result"] or ""))},
        "scripts": scripts,
        "skills": {"suite": sum(1 for c in skill_calls if in_suite(c)),
                   "non_suite": sum(1 for c in skill_calls if not in_suite(c)),
                   "denied": sum(1 for c in skill_calls if c["denied"])},
        "workload_model": {"read_calls": len(reads),
                           "bash_calls": sum(1 for c in bash if "qa-performance/references/workload-model.md"
                                             in str(c["input"].get("command", "")))},
        "tools_beyond_allow": end.get("tools_beyond_allow"),
    }
