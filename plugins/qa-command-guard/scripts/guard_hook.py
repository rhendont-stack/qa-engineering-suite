"""PreToolUse hook of the qa-command-guard plugin: checks a Bash command with the kernel's own command guard.

Claude Code writes the hook input as JSON on stdin. The hook is active only while a QA workspace is: it looks for a
``.qa/config.json`` in the input's ``cwd`` and its parents, with the kernel's own ``find_workspace``, and says nothing
when there is none. In a workspace it classifies the command with ``guard.classify`` (declared activity
``static-analysis``, the least one, so every rule that adds an activity shows up as a grant) and answers:

* guard ``deny``          -> ``deny``, reason ``qa-command-guard: <rule id>: <reason>``
* guard ``require_grant`` -> ``ask``, reason naming the activities the command needs
* guard ``allow``         -> no output: the host's own permission flow applies

A call of the suite's launcher (``python3 <dir>/scripts/qa.py ...``) with no shell metacharacter gets no decision either:
``qa run`` applies the same guard to what it runs. Input the hook cannot read, a config or policy the kernel refuses, and
any other exception give ``ask``. A command over ``MAX_COMMAND_BYTES`` gets ``ask`` at once: the guard's time grows faster
than the command's length (measured, see the README), and the host gives the hook 10 seconds. Where ``SIGALRM`` exists the
check also runs under a ``DEADLINE_S`` timer and asks with reason ``deadline`` when it expires. The exit status is always
0, and nothing is written to stderr.

The kernel is the vendored copy in ``_kernel/`` beside this file (``tools/vendor_kernel.py`` keeps it current).
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, TextIO

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "_kernel"))  # -I ignores PYTHONPATH; this is the only source

try:
    from qa import config, guard, workspace
    from qa.canonical import loads
    _IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # the hook must still answer, so the failure is reported as an ask
    config = guard = workspace = loads = None  # type: ignore[assignment]
    _IMPORT_ERROR = type(exc).__name__

ACTIVITY = "static-analysis"
MAX_COMMAND_BYTES = 4096    # the largest measured size whose slowest command shape took 2 s or less on the slower image
MAX_INPUT_BYTES = 64 * 1024  # the whole stdin payload, read in bytes
DEADLINE_S = 5.0             # half of the 10 s the host allows
# The shell's own metacharacters, and the ones it expands in a word (pattern, brace, tilde, history, comment): a command
# holding any of them is classified as a whole string, never passed through as a plain launcher call.
METACHARACTERS = frozenset(";&|$()<>`\\\n\r*?[]{}~!#")


class _Deadline(BaseException):
    """Raised by the timer; not an ``Exception``, so no handler inside the kernel can swallow it."""


def answer(verdict: str, reason: str) -> Dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": verdict,
                                   "permissionDecisionReason": reason}}


def could_not_check(error: object) -> Dict[str, Any]:
    name = error if isinstance(error, str) else type(error).__name__
    return answer("ask", "qa-command-guard could not check this command: %s" % name)


def find_workspace(start: Path) -> Optional["workspace.Workspace"]:
    """The kernel's workspace for ``start`` (the nearest ancestor holding ``.qa/config.json``), or None when there is none."""
    try:
        return workspace.find_workspace(Path(start))
    except Exception as exc:
        if getattr(exc, "code", None) == "no_workspace":
            return None
        raise


def launcher_call(command: str, cwd: Path) -> bool:
    """True for ``python3|python <dir>/scripts/qa.py ...`` with no shell metacharacter, where the kernel sits beside it."""
    if any(ch in METACHARACTERS for ch in command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 2 or words[0] not in ("python3", "python"):
        return False
    path = Path(words[1])
    path = path if path.is_absolute() else Path(cwd) / path
    return path.name == "qa.py" and path.parent.name == "scripts" and path.is_file() \
        and (path.parent / "_kernel" / "qa" / "guard.py").is_file()


def workspace_config(ws: "workspace.Workspace") -> dict:
    """The workspace's config as ``qa guard check`` reads it: validated, or the raw object for a newer schema."""
    if workspace.check_schema_version(ws, for_write=False) == "schema_newer":
        return loads(workspace.read_config(ws))
    return config.load_config(ws)


def _on_alarm(signum: int, frame: object) -> None:
    raise _Deadline()


def decide(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The hook output for one input, or None for no decision."""
    timer = hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")
    previous = None
    armed = False
    try:
        if timer:
            try:
                previous = signal.signal(signal.SIGALRM, _on_alarm)
                signal.setitimer(signal.ITIMER_REAL, DEADLINE_S)
                armed = True
            except ValueError:  # not the main thread: the size cap alone applies
                previous = None
        return _decide(payload)
    except _Deadline:
        return could_not_check("deadline")
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous if previous is not None else signal.SIG_DFL)


def _decide(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if _IMPORT_ERROR is not None:
            return could_not_check(_IMPORT_ERROR)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("tool_name"), str):
            return could_not_check("InvalidInput")
        if payload["tool_name"] != "Bash":
            return None
        cwd = payload.get("cwd")
        where = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
        ws = find_workspace(where)
        if ws is None:
            return None  # no QA work is active here
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
        if not isinstance(command, str) or not command.strip():
            return could_not_check("InvalidInput")
        size = len(command.encode("utf-8", "surrogatepass"))
        if size > MAX_COMMAND_BYTES:
            return could_not_check("InputTooLarge (%d bytes; the limit is %d)" % (size, MAX_COMMAND_BYTES))
        if launcher_call(command, where):
            return None
        verdict = guard.classify(command, shell=True, ws_root=ws.root, qa_dir=ws.qa_dir, config=workspace_config(ws),
                                 declared_activity=ACTIVITY, cwd=where)
        if verdict.decision == "deny":
            return answer("deny", "qa-command-guard: %s: %s" % (verdict.rule_id, verdict.reason))
        if verdict.decision == "require_grant":
            needs = [a for a in verdict.required_activities if a != ACTIVITY] or list(verdict.required_activities)
            return answer("ask", "qa-command-guard: %s: this command needs a grant for %s. %s" % (
                verdict.rule_id, ", ".join(needs), verdict.reason))
        return None
    except Exception as exc:
        return could_not_check(exc)


def main(stdin: Any = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    try:
        source = getattr(stdin, "buffer", stdin)  # bytes where the stream has them, so the cap means bytes
        data = source.read(MAX_INPUT_BYTES + 1)
        if isinstance(data, str):
            data = data.encode("utf-8", "surrogatepass")
        if len(data) > MAX_INPUT_BYTES:
            out: Optional[Dict[str, Any]] = could_not_check(
                "InputTooLarge (over %d bytes; the limit is %d)" % (MAX_INPUT_BYTES, MAX_INPUT_BYTES))
        else:
            try:
                payload = json.loads(data.decode("utf-8"))
            except ValueError:  # includes UnicodeDecodeError
                payload = None
            out = decide(payload) if isinstance(payload, dict) else could_not_check("InvalidInput")
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    except Exception as exc:
        try:
            stdout.write(json.dumps(could_not_check(exc)) + "\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
