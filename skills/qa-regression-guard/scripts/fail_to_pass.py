"""Propose the FAIL_TO_PASS tests of a recorded fix, and the kernel command that revalidates it.

Usage (from the project root, after ``fix add`` and a recorded run at the fix's base commit and at its fix commit):
    python3 fail_to_pass.py --fix FIX-001 --base-run EXE-001 --head-run EXE-002

Prints one JSON object:

    status               "proposed" when the fix's findings name a recorded test that failed by assertion on the base run;
                         otherwise "unverified", and the script exits 1
    defect_tests         the proposed FAIL_TO_PASS tests: only tests the fix's findings name, never a test chosen another way
    source               "named by the fix's findings", or None
    other_base_failures  [{test, base, head}]  tests that failed on the base run and were not proposed
    not_proposed         [{test, why}]  tests the fix names or changes that cannot show the defect, with the reason
    fix_commits          {base, fix}  the fix record's commits
    command              the ``revalidate`` call to run, written out; None when status is "unverified"
    warnings, note

A test that merely failed on the base run, or that the fix changed, is not evidence about the finding: an unrelated or flaky
test, or one the fix loosened, would then verify a finding it never exercised. So with no test tied to the finding there is no
command: the fix stays unverified until a test that shows the defect is named in the finding (``key_attributes.test``).

The script judges nothing. It reads the state through the launcher's ``state show`` and the fix's diff through the kernel's
neutralised git, and only proposes: the outcome is what the kernel's ``revalidate`` derives from the recorded runs. It records
nothing and writes nothing. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _change  # noqa: E402
from _change import ChangeError, is_test_file, language_of, parse_diff, python_module, test_function_ranges  # noqa: E402

SOURCE = "named by the fix's findings"
RUN_FROM = "Run from the project root:"
NOTE = "a proposal only: the kernel's revalidate derives the outcome from the recorded runs"
UNVERIFIED = ("the fix is unverified: no recorded test that the fix's findings name failed by assertion on the base run, so no "
              "test is tied to the defect and there is nothing to revalidate. Record a test that shows the defect at the base "
              "commit and name it in the finding (key_attributes.test), then run this again")


# --------------------------------------------------------------------------- the recorded runs

def tests_of(st: Dict[str, Any], execution: str) -> Dict[str, Dict[str, Any]]:
    """{test id: {"status", "kinds"}}: a test that failed in any attempt counts as failed, with its failure kinds."""
    tests: Dict[str, Dict[str, Any]] = {}
    for result in st.get("results", {}).values():
        if result.get("execution") != execution:
            continue
        entry = tests.setdefault(result["test_id"], {"statuses": set(), "kinds": set()})
        entry["statuses"].add(result.get("status"))
        if result.get("status") == "failed":
            entry["kinds"].add(result.get("failure_kind"))
    out = {}
    for test, entry in tests.items():
        statuses = entry["statuses"]
        status = "failed" if "failed" in statuses else ("passed" if "passed" in statuses else sorted(s or "" for s in statuses)[0])
        out[test] = {"status": status, "kinds": entry["kinds"]}
    return out


def _by_assertion(entry: Optional[Dict[str, Any]]) -> bool:
    return entry is not None and entry["status"] == "failed" and entry["kinds"] == {"assertion"}


def named_tests(st: Dict[str, Any], finding_ids: Iterable[str], known: Set[str]) -> List[str]:
    """Recorded test ids a finding names in its ``key_attributes`` or its evidence."""
    named: Set[str] = set()
    for fid in finding_ids:
        finding = st.get("findings", {}).get(fid) or {}
        values: List[Any] = list((finding.get("key_attributes") or {}).values()) + list(finding.get("evidence") or [])
        for value in values:
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, str) and item in known:
                    named.add(item)
    return sorted(named)


def _commit(st: Dict[str, Any], execution: str) -> Optional[str]:
    git = (st.get("executions", {}).get(execution) or {}).get("git")
    return git.get("commit") if isinstance(git, dict) else None


def select(st: Dict[str, Any], fix_id: str, base_exe: str, head_exe: str, changed_tests: Optional[Set[str]] = None,
           launcher: str = ".claude/skills/qa-regression-guard/scripts/qa.py") -> Dict[str, Any]:
    fix = st.get("fixes", {}).get(fix_id)
    if fix is None:
        raise ChangeError("unknown_fix", "the state holds no such fix", fix=fix_id)
    for exe in (base_exe, head_exe):
        if exe not in st.get("executions", {}):
            raise ChangeError("unknown_execution", "the state holds no such execution", execution=exe)
    changed = set(changed_tests or ())
    warnings: List[str] = []
    if _commit(st, base_exe) != fix.get("base_commit"):
        warnings.append("the base run %s is at %s, not at the fix's base commit %s: the kernel will refuse it"
                        % (base_exe, _commit(st, base_exe), fix.get("base_commit")))
    if _commit(st, head_exe) != fix.get("fix_commit"):
        warnings.append("the head run %s is at %s, not at the fix commit %s: the kernel will refuse it"
                        % (head_exe, _commit(st, head_exe), fix.get("fix_commit")))
    base, head = tests_of(st, base_exe), tests_of(st, head_exe)
    known = set(base) | set(head)
    base_failures = sorted(t for t in base if _by_assertion(base[t]))
    not_proposed: Dict[str, str] = {}

    named = named_tests(st, fix.get("findings") or [], known)
    defect: List[str] = []
    source: Optional[str] = None
    if named:
        defect = [t for t in named if _by_assertion(base.get(t))]
        for test in named:
            if test not in defect:
                not_proposed[test] = _why_not(base.get(test))
        if defect:
            source = SOURCE
        else:
            warnings.append("the tests the findings name did not fail by assertion on the base run, so they cannot show the defect")
    else:
        warnings.append("the fix's findings name no recorded test")
    for test in sorted(changed - set(defect)):
        if test not in not_proposed and test not in base_failures:
            not_proposed[test] = _why_not(base.get(test))
    for test in sorted(set(head) - set(base)):
        not_proposed.setdefault(test, "absent from the base run")

    others = [{"test": t, "base": base[t]["status"], "head": head[t]["status"] if t in head else "absent"}
              for t in sorted(t for t in base if base[t]["status"] == "failed" and t not in defect)]
    command = None
    note = NOTE
    status = "proposed" if defect else "unverified"
    if defect:
        words = ["python3", launcher, "--role", "verifier", "--skill", "qa-regression-guard", "revalidate", fix_id,
                 "--f2p", "%s:%s" % (base_exe, head_exe), "--p2p", "%s:%s" % (base_exe, head_exe)]
        for test in defect:
            words += ["--defect-test", test]
        command = " ".join(shlex.quote(w) for w in words)
    else:
        note = UNVERIFIED
    return {"fix": fix_id, "status": status, "base_run": base_exe, "head_run": head_exe, "defect_tests": defect, "source": source,
            "other_base_failures": others, "not_proposed": [{"test": t, "why": not_proposed[t]} for t in sorted(not_proposed)],
            "fix_commits": {"base": fix.get("base_commit"), "fix": fix.get("fix_commit")},
            "run_from": RUN_FROM if command else None, "command": command,
            "warnings": warnings, "note": note}


def _why_not(entry: Optional[Dict[str, Any]]) -> str:
    if entry is None:
        return "absent from the base run"
    if entry["status"] == "failed":
        kinds = sorted(k or "unknown" for k in entry["kinds"])
        return "failed on the base run by %s, not by assertion" % ", ".join(kinds)
    return "did not fail on the base run (%s), so it cannot show the defect" % entry["status"]


# --------------------------------------------------------------------------- the tests a fix changes

def changed_tests(repo: "_change.Repo", base: str, fix: str, known: Set[str]) -> Set[str]:
    """Recorded test ids whose test function the fix's diff touches (Python, by function); for another language, every
    recorded test of a test file the diff touches."""
    entries = [e for e in parse_diff(repo.diff(base, fix)) if is_test_file(e["path"]) and e["status"] != "D"]
    texts = repo.blobs(fix, [e["path"] for e in entries if e["path"].endswith(".py")])
    found: Set[str] = set()
    for entry in entries:
        path = entry["path"]
        lines = {l["new"] for h in entry["hunks"] for l in h["lines"] if l["kind"] == "+" and l["new"] is not None}
        lines |= {l["new"] for h in entry["hunks"] for i, l in enumerate(h["lines"]) if l["kind"] == " " and
                  any(x["kind"] == "-" for x in h["lines"][max(0, i - 1):i + 2])}
        if path.endswith(".py") and path in texts:
            module = python_module(path) or ""
            names = {name for name, first, last in test_function_ranges(texts[path]) if any(first <= n <= last for n in lines)}
            for test in known:
                head, _, func = test.rpartition("::")
                if func in names and (head == module or head.startswith(module + ".")):
                    found.add(test)
        elif language_of(path) is not None:
            stem = os.path.splitext(path)[0]
            for test in known:
                if stem in test or stem.replace("/", ".") in test:
                    found.add(test)
    return found


# --------------------------------------------------------------------------- command line

def read_state(launcher: str) -> Dict[str, Any]:
    try:
        done = subprocess.run([sys.executable, "-I", launcher, "--role", "verifier", "--skill", "qa-regression-guard", "state", "show"],
                              capture_output=True, timeout=300, cwd=os.getcwd())
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChangeError("state_unavailable", "the launcher could not run: %s" % exc)
    try:
        reply = json.loads(done.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise ChangeError("state_unavailable", "the launcher's reply is not JSON")
    if not reply.get("ok") or not isinstance(reply.get("state"), dict):
        raise ChangeError("state_unavailable", "state show failed", reply_error=reply.get("error"))
    return reply["state"]


def shown_launcher() -> str:
    """The launcher as the agent calls it: always relative to the project root, whatever the working directory (a worktree too)."""
    return ".claude/skills/qa-regression-guard/scripts/qa.py"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Propose the FAIL_TO_PASS tests of a recorded fix.")
    parser.add_argument("--fix", required=True, help="the fix record, FIX-NNN")
    parser.add_argument("--base-run", required=True, help="the execution at the fix's base commit")
    parser.add_argument("--head-run", required=True, help="the execution at the fix commit")
    args = parser.parse_args(argv)
    try:
        st = read_state(_change.LAUNCHER)
        fix = (st.get("fixes") or {}).get(args.fix)
        if fix is None:
            raise ChangeError("unknown_fix", "the state holds no such fix", fix=args.fix)
        known = set(tests_of(st, args.base_run)) | set(tests_of(st, args.head_run))
        changed: Set[str] = set()
        warnings: List[str] = []
        try:
            repo = _change.Repo(os.getcwd())
            changed = changed_tests(repo, repo.resolve(fix["base_commit"]), repo.resolve(fix["fix_commit"]), known)
        except ChangeError as error:
            warnings.append("the fix's diff could not be read (%s), so changed tests were not used" % error.code)
        out = select(st, args.fix, args.base_run, args.head_run, changed_tests=changed, launcher=shown_launcher())
    except ChangeError as error:
        return _change.refuse(error)
    out["warnings"] = warnings + out["warnings"]
    out["ok"] = True
    _change.emit(out)
    return 0 if out["status"] == "proposed" else 1


if __name__ == "__main__":
    sys.exit(main())
