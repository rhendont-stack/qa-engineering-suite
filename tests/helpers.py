from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "shared" / "kernel"
CORPUS = REPO / "tests" / "corpus"
LAUNCHER = REPO / "shared" / "launcher" / "qa.py"

# python -c code: writes a one-case JUnit report to argv[1]; argv[2] ("pass" or "fail") sets the outcome.
WRITE_JUNIT = ("import sys, pathlib; out, outcome = sys.argv[1], sys.argv[2]; body = '<failure/>' if outcome == 'fail' else ''\n"
               "pathlib.Path(out).write_text(f'<testsuite><testcase classname=\"c\" name=\"t\">{body}</testcase></testsuite>')\n"
               "sys.exit(1 if outcome == 'fail' else 0)")


def req(py, *args, **kw):
    """A RunRequest for ``python -c <args>`` that declares ``{evidence}/junit.xml`` as its report."""
    from qa import execution

    return execution.RunRequest(command=py.argv(*args), reports=(("junit", "{evidence}/junit.xml"),), **kw)
