from __future__ import annotations

import os
import signal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "shared" / "kernel"
CORPUS = REPO / "tests" / "corpus"
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


def replace_file(path, how, scratch):
    """Stand ``NOT_A_REGULAR_FILE[how]`` where the regular file at ``path`` is."""
    copy = scratch / "copy-of-the-file"
    copy.write_bytes(path.read_bytes())
    path.unlink()
    NOT_A_REGULAR_FILE[how](path, copy)


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
