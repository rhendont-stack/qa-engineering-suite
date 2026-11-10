from __future__ import annotations

import json
import os
import re
import signal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "shared" / "kernel"
CORPUS = REPO / "tests" / "corpus"
FIXTURES = REPO / "fixtures"
ANSWER_KEYS = REPO / "benchmark" / "answer-keys"
LAUNCHER = REPO / "shared" / "launcher" / "qa.py"

# python -c code: writes a one-case JUnit report to argv[1]; argv[2] ("pass" or "fail") sets the outcome.
WRITE_JUNIT = ("import sys, pathlib; out, outcome = sys.argv[1], sys.argv[2]; body = '<failure/>' if outcome == 'fail' else ''\n"
               "pathlib.Path(out).write_text(f'<testsuite><testcase classname=\"c\" name=\"t\">{body}</testcase></testsuite>')\n"
               "sys.exit(1 if outcome == 'fail' else 0)")


# What can stand where a regular file was recorded. Each takes the path and a copy of the file's old content.
NOT_A_REGULAR_FILE = {
    "fifo": lambda path, _copy: os.mkfifo(str(path)),
    "symlink_to_a_device": lambda path, _copy: path.symlink_to("/dev/zero"),
    "symlink_to_the_same_content": lambda path, copy: path.symlink_to(copy),
    "dangling_symlink": lambda path, copy: path.symlink_to(str(copy) + ".gone"),
    "directory": lambda path, _copy: path.mkdir(),
}
# Only a check that reads in chunks may meet the device. ``read_bytes`` on /dev/zero never returns to the interpreter,
# so the alarm in ``finishes`` cannot stop it, and it grows until the machine runs out of memory.
WHOLE_READ_SAFE = sorted(set(NOT_A_REGULAR_FILE) - {"symlink_to_a_device"})


def answer_key(name):
    """The hidden key of one fixture, which lives only under benchmark/answer-keys/."""
    return json.loads((ANSWER_KEYS / (name + ".json")).read_text(encoding="utf-8"))


def replace_file(path, how, scratch):
    """Stand ``NOT_A_REGULAR_FILE[how]`` where the regular file at ``path`` is."""
    copy = scratch / "copy-of-the-file"
    copy.write_bytes(path.read_bytes())
    path.unlink()
    NOT_A_REGULAR_FILE[how](path, copy)


def write_event_at(ws, actor, ts, type_, data, signed=True):
    """A chained event written straight to ``actor``'s session file at ``ts``, with none of ``ledger.append``'s ordering.

    It stands for a ledger written before timestamps were kept in order, an imported bundle or a hand edit, which is
    the only way to get events out of order now. ``ts`` is a datetime, or a string for a timestamp no clock would give.
    An event type that needs a MAC gets a real one from this workspace's key; ``signed=False`` writes the event as a
    workspace that predates machine-bound authority holds it.
    """
    from qa import canonical, ids

    if signed:
        from qa import authority

        if type_ in authority.EVENTS:
            data = dict(data, authority_mac=authority.sign(ws, type_, data))

    path = ws.path("ledger", actor.session + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_bytes().splitlines() if path.exists() else []
    head = json.loads(lines[-1]) if lines else None
    envelope = {
        "v": 1,
        "seq": head["seq"] + 1 if head else 1,
        "id": ids.new_id("EVT"),
        "ts": ts if isinstance(ts, str) else ids.format_ts(ts),
        "type": type_,
        "actor": {"role": actor.role, "skill": actor.skill, "session": actor.session},
        "data": data,
        "prev": head["hash"] if head else None,
    }
    envelope["hash"] = canonical.hash_obj(envelope)
    with open(str(path), "ab") as handle:
        handle.write(canonical.dumps(envelope) + b"\n")
    return envelope


def without_mac(data):
    """``data`` as the caller gave it, after checking the ledger added a well-formed MAC to it."""
    assert re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", data["authority_mac"])
    return {name: value for name, value in data.items() if name != "authority_mac"}


# How a hostile repository's own git configuration can name a program that a read of the tree would run (C1).
HOSTILE_GIT = ("local_filter", "include_path", "config_worktree", "submodule")


def _git_in(root, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always", *args],
                          check=True, capture_output=True)


def _committed_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    _git_in(root, "init", "-q")
    (root / "tracked.txt").write_text("1")
    _git_in(root, "add", "tracked.txt")
    _git_in(root, "commit", "-q", "-m", "c1")


def hostile_git_repo(root, marker, how="local_filter"):
    """Make ``root`` a git repository whose own configuration names a filter driver that touches ``marker`` (then acts as
    ``cat``), with every file attributed to it and one tracked file modified, so ``git status`` or ``git diff`` would run it.

    ``how`` says where the driver is configured: ``.git/config`` itself, a file it includes, ``config.worktree``, or the
    ``.git/config`` of a submodule. Each is data in the repository: a clone or a copied tree carries it."""
    import shlex

    program = "sh -c %s" % shlex.quote("touch %s; cat" % shlex.quote(str(marker)))
    _committed_repo(root)
    target = root
    if how == "submodule":
        origin = Path(str(root) + "-submodule-origin")
        _committed_repo(origin)
        _git_in(root, "submodule", "add", "-q", str(origin), "sub")
        _git_in(root, "commit", "-q", "-m", "add the submodule")
        target = root / "sub"
    (target / ".gitattributes").write_text("* filter=x\n")
    if how == "include_path":
        git_dir = root / ".git"
        _git_in(root, "config", "-f", str(git_dir / "hostile.inc"), "filter.x.clean", program)
        _git_in(root, "config", "include.path", "hostile.inc")
    elif how == "config_worktree":
        _git_in(root, "config", "extensions.worktreeConfig", "true")
        _git_in(root, "config", "--worktree", "filter.x.clean", program)
    else:
        _git_in(target, "config", "filter.x.clean", program)
    (target / "tracked.txt").write_text("2")
    return root


def finishes(check, seconds=5):
    """``check()``, failing instead of hanging: opening a FIFO blocks for ever and ``/dev/zero`` never ends."""
    def expired(*_args):
        raise AssertionError("still running after %d s" % seconds)

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        return check()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def req(py, *args, **kw):
    """A RunRequest for ``python -c <args>`` that declares ``{evidence}/junit.xml`` as its report."""
    from qa import execution

    return execution.RunRequest(command=py.argv(*args), reports=(("junit", "{evidence}/junit.xml"),), **kw)


def base(**over):
    """A valid finding in Appendix A.3's field names; ``over`` replaces whole fields."""
    f = {"title": "IDOR on /orders/{id}", "description": "Any user can read another user's order.", "category": "security",
         "severity_rationale": {"anchor": "SEC-H1", "modifiers": []},
         "component": {"path": "app/orders/views.py", "endpoint": "GET /orders/{id}", "criticality": "critical"},
         "verification_status": "suspected", "evidence": [], "reproduction_steps": [],
         "refs": {"cwe": ["CWE-639"], "owasp_api_2023": ["API1:2023"], "rule": "manual", "location": "app/orders/views.py"}}
    f.update(over)
    return f


# python -c code: writes a one-alert SARIF report to argv[1] and exits 1.
WRITE_SARIF = ("import json, sys; json.dump({'version': '2.1.0', 'runs': [{'tool': {'driver': {'name': 'scanner', 'rules': [{'id': 'R1'}]}}, "
               "'results': [{'ruleId': 'R1', 'level': 'error', 'message': {'text': 'm'}, 'locations': [{'physicalLocation': "
               "{'artifactLocation': {'uri': 'a.py'}, 'region': {'startLine': 1}}}]}]}]}, open(sys.argv[1], 'w')); sys.exit(1)")


class FakeEngine:
    """An ``Engine`` for the ``docker`` shape whose spawns are recorded and answered here, so nothing is started.

    ``calls`` holds the arguments of each spawn after the engine prefix, ``spawns`` the whole ``run_process`` call, ``reply``
    sets what every later spawn returns, and ``when`` answers the spawns its predicate picks (the latest rule wins; ``times``
    uses it up).
    """

    def __init__(self, engine):
        self.engine = engine
        self.calls = []
        self.spawns = []
        self._reply = {}
        self._rules = []

    def reply(self, **over):
        self._reply = over

    def when(self, picks, times=None, **over):
        self._rules.insert(0, [picks, times, over])

    def networks(self):
        """The ``--network`` value of each container started, in order."""
        return [arg.split("=", 1)[1] for call in self.calls if call[0] in ("run", "create") for arg in call
                if arg.startswith("--network=")]

    def _answer(self, args):
        for rule in self._rules:
            if rule[1] != 0 and rule[0](args):
                rule[1] = rule[1] - 1 if rule[1] is not None else None
                return rule[2]
        return self._reply

    def run_process(self, command, **kw):
        from qa import runner

        args = list(command[len(self.engine.argv):])
        self.calls.append(args)
        stream = kw.get("stdin_path")
        self.spawns.append(dict(kw, command=list(command), stdin_bytes=stream.read_bytes() if stream and stream.is_file() else None))
        base = dict(exit_code=0, signal=None, timed_out=False, spawn_error=None, started_at="2026-01-01T00:00:00.000Z",
                    ended_at="2026-01-01T00:00:01.000Z", duration_ms=1000,
                    stdout=b"", stderr=b"", stdout_dropped=0, stderr_dropped=0, orphaned_output=False)
        base.update(self._answer(args))
        return runner.RunOutcome(**base)


@pytest.fixture
def fake_engine(monkeypatch):
    from qa import container, runner

    fake = FakeEngine(container.Engine("docker"))
    monkeypatch.setattr(runner, "run_process", fake.run_process)
    return fake
