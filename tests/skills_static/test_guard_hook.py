"""L0 tests for the qa-command-guard plugin's PreToolUse hook (plugins/qa-command-guard/scripts/guard_hook.py).

The hook routes Bash commands through the kernel's own guard while a QA workspace is active. The guard's rules are
exercised by ``tests/corpus/guard/cases.json``; these tests check how the hook maps the guard's answer to a hook decision,
when it stays silent and what it does with input it cannot read.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shlex
import subprocess
import sys
import time

import pytest

from helpers import CORPUS, REPO

PLUGIN = REPO / "plugins" / "qa-command-guard"
HOOK = PLUGIN / "scripts" / "guard_hook.py"
CASES = [c for c in json.loads((CORPUS / "guard" / "cases.json").read_text(encoding="utf-8")) if c["shell"]]
JOINED = [c for c in CASES if c["expect"] == "deny"]


def load_hook():
    spec = importlib.util.spec_from_file_location("guard_hook_under_test", str(HOOK))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hook():
    return load_hook()


def payload(command, cwd, tool="Bash"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}


def decision(out):
    """(permissionDecision, reason) of a hook output, with the envelope checked."""
    body = out["hookSpecificOutput"]
    assert set(out) == {"hookSpecificOutput"} and body["hookEventName"] == "PreToolUse"
    return body["permissionDecision"], body["permissionDecisionReason"]


def run_hook(stdin_text, cwd):
    r = subprocess.run([sys.executable, "-I", str(HOOK)], input=stdin_text, capture_output=True, text=True, cwd=str(cwd), timeout=60)
    return r.returncode, r.stdout, r.stderr


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_hook_denies_and_asks_from_the_guard_corpus(hook, ws, case):
    out = hook.decide(payload(case["command"], ws.root))
    if case["expect"] == "deny":
        verdict, reason = decision(out)
        assert verdict == "deny" and reason.startswith("qa-command-guard: %s: " % case["rule_id"])
    elif case["expect"] == "require_grant":
        verdict, reason = decision(out)
        assert verdict == "ask"
        for activity in case["adds"]:
            assert activity in reason
    else:
        # the hook declares static-analysis, so an allow that rests on the declared activity covering a rule is a grant here
        assert out is None or decision(out)[0] == "ask"


def test_hook_declares_the_least_activity(hook, ws):
    assert decision(hook.decide(payload("pytest -q", ws.root)))[0] == "ask"          # running project code is a grant above static-analysis
    assert "test-execution-local" in decision(hook.decide(payload("pytest -q", ws.root)))[1]
    assert hook.decide(payload("git status", ws.root)) is None                      # a read-only tool stays with the host's own flow


def test_hook_is_inactive_without_a_workspace(hook, tmp_path):
    denied = JOINED[0]["command"]
    assert hook.find_workspace(tmp_path) is None
    assert hook.decide(payload(denied, tmp_path)) is None
    code, out, _ = run_hook(json.dumps(payload(denied, tmp_path)), tmp_path)
    assert (code, out) == (0, "")


def test_hook_finds_the_nearest_workspace_from_a_subdirectory(hook, ws):
    inner = ws.root / "a" / "b"
    inner.mkdir(parents=True)
    found = hook.find_workspace(inner)
    assert found is not None and found.root == ws.root and found.qa_dir == ws.qa_dir
    assert decision(hook.decide(payload(JOINED[0]["command"], inner)))[0] == "deny"


def test_hook_runs_as_the_plugin_runs_it(ws):
    denied = JOINED[0]
    code, out, err = run_hook(json.dumps(payload(denied["command"], ws.root)), ws.root)
    assert code == 0 and err == ""
    verdict, reason = decision(json.loads(out))
    assert verdict == "deny" and "qa-command-guard: %s" % denied["rule_id"] in reason


def test_hook_ignores_other_tools(hook, ws):
    assert hook.decide(payload(JOINED[0]["command"], ws.root, tool="Read")) is None
    assert hook.decide({"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": "x"}, "cwd": str(ws.root)}) is None


def test_hook_unreadable_input_asks(hook, ws):
    def asks(out):
        verdict, reason = decision(out)
        assert verdict == "ask" and reason.startswith("qa-command-guard could not check this command: ")

    asks(hook.decide(payload(["pytest", "-q"], ws.root)))                    # a list, not a string
    asks(hook.decide(payload("x" * (hook.MAX_COMMAND_BYTES + 1), ws.root)))  # one byte over the limit
    asks(hook.decide({"tool_name": "Bash", "tool_input": {}, "cwd": str(ws.root)}))
    asks(hook.decide({"tool_name": "Bash", "tool_input": "pytest", "cwd": str(ws.root)}))
    asks(hook.decide(["not", "an", "object"]))
    asks(hook.decide({"tool_input": {"command": "ls"}, "cwd": str(ws.root)}))             # no tool name
    for name in (None, 7, ["Bash"], {"Bash": 1}):                                         # a tool name that is not a string
        asks(hook.decide({"tool_name": name, "tool_input": {"command": "ls"}, "cwd": str(ws.root)}))
    assert hook.decide({"tool_name": "bash", "tool_input": {"command": "ls"}, "cwd": str(ws.root)}) is None  # a string that is not Bash
    assert hook.decide(payload("# " + "x" * (hook.MAX_COMMAND_BYTES - 2), ws.root)) is None   # exactly the limit is read
    assert hook.decide(payload("# " + "é" * ((hook.MAX_COMMAND_BYTES - 2) // 2), ws.root)) is None          # the limit counts bytes
    asks(hook.decide(payload("# " + "é" * ((hook.MAX_COMMAND_BYTES - 2) // 2 + 1), ws.root)))
    for text in ("not json", "", "[1]", '{"tool_name": "Bash"', "\x00"):
        out = io.StringIO()
        assert hook.main(io.StringIO(text), out) == 0
        asks(json.loads(out.getvalue()))
    out = io.StringIO()
    assert hook.main(io.StringIO(json.dumps(payload("pytest -q", ws.root, tool="Read"))), out) == 0 and out.getvalue() == ""


def test_hook_reads_its_input_as_exact_utf8_bytes(hook, ws):
    def ask_for(raw):
        source = io.TextIOWrapper(io.BytesIO(raw), encoding="latin-1")   # .buffer holds the bytes as sent
        out = io.StringIO()
        assert hook.main(source, out) == 0
        return decision(json.loads(out.getvalue()))

    good = json.dumps(payload("ls", ws.root)).encode()
    verdict, reason = ask_for(good + b" " * hook.MAX_INPUT_BYTES)
    assert verdict == "ask" and "InputTooLarge" in reason and str(hook.MAX_INPUT_BYTES) in reason
    assert ask_for(b'{"tool_name": "Bash", "x": "\xff\xfe"}')[0] == "ask"      # not UTF-8
    big = json.dumps(payload("ls", ws.root)).encode()
    assert len(big) <= hook.MAX_INPUT_BYTES
    out = io.StringIO()
    assert hook.main(io.TextIOWrapper(io.BytesIO(big), encoding="utf-8"), out) == 0 and out.getvalue() == ""


def test_hook_asks_when_the_check_outlasts_its_deadline(hook, ws, monkeypatch):
    import signal

    if not hasattr(signal, "SIGALRM"):
        pytest.skip("no SIGALRM here: the size cap alone applies")

    def slow(*args, **kwargs):
        while True:   # busy, as the guard is on a command it takes long over
            pass

    monkeypatch.setattr(hook.guard, "classify", slow)
    monkeypatch.setattr(hook, "DEADLINE_S", 0.3)
    before = signal.getsignal(signal.SIGALRM)
    out = io.StringIO()
    start = time.monotonic()
    assert hook.main(io.StringIO(json.dumps(payload("pytest -q", ws.root))), out) == 0
    took = time.monotonic() - start
    assert decision(json.loads(out.getvalue())) == ("ask", "qa-command-guard could not check this command: deadline")
    assert took < 0.3 + 2.0, took
    assert signal.getsignal(signal.SIGALRM) == before and signal.getitimer(signal.ITIMER_REAL)[0] == 0   # timer cleared, handler restored
    monkeypatch.undo()
    assert hook.decide(payload("git status", ws.root)) is None   # and the next call is unaffected


@pytest.mark.parametrize("shape", ["and-chain", "semicolons", "pipes", "newlines", "one-long-argument"])
def test_hook_answers_a_cap_sized_command_inside_its_deadline(hook, ws, shape):
    unit, joiner = {"and-chain": ("ls a", " && "), "semicolons": ("ls a", "; "), "pipes": ("ls a", " | "),
                    "newlines": ("ls a", "\n"), "one-long-argument": ("ls " + "a" * 400, " ")}[shape]
    command = joiner.join([unit] * ((hook.MAX_COMMAND_BYTES - 8) // (len(unit) + len(joiner))))
    assert hook.MAX_COMMAND_BYTES - 64 < len(command.encode()) <= hook.MAX_COMMAND_BYTES
    start = time.monotonic()
    code, out, err = run_hook(json.dumps(payload(command, ws.root)), ws.root)
    took = time.monotonic() - start   # measured in this run, on this image
    assert code == 0 and err == "" and took < hook.DEADLINE_S, took
    if out:
        assert decision(json.loads(out))[0] in ("ask", "deny")


def test_hook_over_the_cap_asks_at_once(hook, ws):
    command = "ls a; " * (hook.MAX_COMMAND_BYTES // 6 + 1)
    assert len(command.encode()) > hook.MAX_COMMAND_BYTES
    start = time.monotonic()
    code, out, _ = run_hook(json.dumps(payload(command, ws.root)), ws.root)
    took = time.monotonic() - start
    verdict, reason = decision(json.loads(out))
    assert code == 0 and verdict == "ask" and str(len(command.encode())) in reason and took < 2.0, (reason, took)


def test_hook_unreadable_config_asks(hook, ws):
    ws.path("config.json").write_text("{not json")
    verdict, reason = decision(hook.decide(payload("pytest -q", ws.root)))
    assert verdict == "ask" and reason.startswith("qa-command-guard could not check this command: ")


def test_hook_never_exits_nonzero(hook, ws, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook.guard, "classify", broken)
    out = io.StringIO()
    assert hook.main(io.StringIO(json.dumps(payload("pytest -q", ws.root))), out) == 0
    assert decision(json.loads(out.getvalue())) == ("ask", "qa-command-guard could not check this command: RuntimeError")


def test_hook_never_exits_nonzero_on_a_closed_stdin(hook):
    class Broken:
        def read(self, *args):
            raise OSError("closed")

    out = io.StringIO()
    assert hook.main(Broken(), out) == 0
    assert decision(json.loads(out.getvalue()))[0] == "ask"


@pytest.fixture
def launcher(ws):
    skill = ws.root / "skills" / "qa-demo" / "scripts"
    (skill / "_kernel" / "qa").mkdir(parents=True)
    (skill / "_kernel" / "qa" / "guard.py").write_text("")
    (skill / "qa.py").write_text("")
    return skill / "qa.py"


def test_hook_launcher_passthrough_needs_a_plain_launcher_call(hook, ws, launcher):
    plain = "python3 %s run --shell -- pytest -q" % shlex.quote(str(launcher))
    assert hook.decide(payload(plain, ws.root)) is None
    assert hook.decide(payload("python %s guard check -- pytest" % shlex.quote(str(launcher)), ws.root)) is None
    assert hook.launcher_call(plain, ws.root)
    joined = JOINED[0]
    joins = {"semicolon": "%s; %s", "and": "%s && %s", "pipe": "%s | %s", "newline": "%s\n%s", "background": "%s & %s",
             "or": "%s || %s"}
    for name, template in joins.items():
        command = template % (plain, joined["command"])
        assert not hook.launcher_call(command, ws.root), name
        verdict, reason = decision(hook.decide(payload(command, ws.root)))
        assert verdict == "deny" and joined["rule_id"] in reason, name
    wrapped = "%s $(%s)" % (plain, joined["command"])
    assert not hook.launcher_call(wrapped, ws.root)
    assert decision(hook.decide(payload(wrapped, ws.root)))[0] == "deny"
    path = shlex.quote(str(launcher))
    expanding = {"pattern-star": "python3 %s run" % path.replace("qa.py", "q*.py"),
                 "pattern-question": "python3 %s run" % path.replace("qa.py", "q?.py"),
                 "pattern-bracket": "python3 %s run" % path.replace("qa.py", "q[a].py"),
                 "brace": "python3 %s run" % path.replace("qa.py", "q{a,b}.py"),
                 "tilde": "python3 ~/scripts/qa.py run", "history-bang": "%s !" % plain,
                 "comment-hash": "%s # note" % plain, "closing-bracket": "%s ]" % plain, "closing-brace": "%s }" % plain}
    for name, command in expanding.items():
        assert not hook.launcher_call(command, ws.root), name
    for command in ("%s `%s`" % (plain, joined["command"]), "%s > out" % plain, "%s < in" % plain, "%s (x)" % plain,
                    "%s $X" % plain):
        assert not hook.launcher_call(command, ws.root), command


def test_hook_launcher_passthrough_needs_the_kernel_beside_the_launcher(hook, ws, launcher, tmp_path):
    (launcher.parent / "_kernel" / "qa" / "guard.py").unlink()
    assert not hook.launcher_call("python3 %s run -- pytest" % shlex.quote(str(launcher)), ws.root)
    other = launcher.parent / "other.py"
    other.write_text("")
    (launcher.parent / "_kernel" / "qa" / "guard.py").write_text("")
    path = shlex.quote(str(launcher))
    for command in ("python3 %s run" % shlex.quote(str(other)), "python3 -c 'import sys'", "python3 -I %s run" % path,
                    "sh %s run" % path, "python3.12 %s run" % path, "FOO=1 python3 %s run" % path, "python3", "",
                    "python3 'unclosed"):
        assert not hook.launcher_call(command, ws.root), command
    assert hook.launcher_call("python3 %s run" % shlex.quote(str(launcher.relative_to(ws.root))), ws.root)   # a relative path reads against cwd


def test_vendored_kernel_is_the_one_the_hook_imports(hook):
    assert (PLUGIN / "scripts" / "_kernel" / "MANIFEST.sha256").is_file()
    assert not (PLUGIN / "scripts" / "qa.py").exists() and not (PLUGIN / "scripts" / "_kernel" / "qa.py").exists()
    assert str(PLUGIN / "scripts" / "_kernel") in sys.path


def test_hooks_json_has_the_documented_shape():
    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert set(doc) == {"description", "hooks"} and doc["description"].strip()
    assert list(doc["hooks"]) == ["PreToolUse"] and len(doc["hooks"]["PreToolUse"]) == 1
    group = doc["hooks"]["PreToolUse"][0]
    assert group["matcher"] == "Bash" and len(group["hooks"]) == 1
    assert group["hooks"][0] == {"type": "command", "command": 'python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/guard_hook.py"',
                                 "timeout": 10}
