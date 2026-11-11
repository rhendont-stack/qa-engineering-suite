"""The converted mutation report reaches the kernel at structured strength, and the mutation_score_min gate reads it.

The flow is the skill's own, through the real launcher and the real converter, on the corpus capture of mutmut 3.8.1: a project
with the capture's sources is committed, the skill is installed under ``.claude/skills/`` (outside the commit, as the harness
installs it), a suite worktree is added, mutmut's ``mutants/`` from the capture is placed in it (a stub ``mutmut`` stands in for
the tool, which this host need not have), and one kernel run in the worktree runs ``mutmut run`` and then the converter, with
the converted file declared as the run's ``mutation`` report. The report is then in that execution's evidence manifest, so
``ingest mutation`` of it is ``structured``, and a gate with ``mutation_score_min`` evaluated at that commit reads its score.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import REPO

SKILL = REPO / "skills" / "qa-mutation"
CAPTURE = REPO / "tests" / "corpus" / "producers" / "mutmut_mutation"
APPROVAL = "This is our own project; I approve running its tests on this machine."
# The skill's mutation run, as SKILL.md writes it: the tool, then the converter, the converted file declared as the report.
CONVERTED = "{evidence}/mutation-report.json"
SHELL = ("mutmut run && python3 ../../../.claude/skills/qa-mutation/scripts/mutmut_to_report.py --root . --out "
         + CONVERTED)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="tool missing: git")


def git(root, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false"] + list(args), check=True, capture_output=True, text=True, env=env)


def environment(tmp_path):
    """PATH with a stub ``mutmut`` (exit 0, as mutmut does whatever survives) and this interpreter as ``python3``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "mutmut"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    (bin_dir / "python3").symlink_to(sys.executable)
    return {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "HOME": str(tmp_path)}


def kernel(work, env, *args, cwd=None, role="verifier", skill="qa-mutation"):
    launcher = work / ".claude" / "skills" / "qa-mutation" / "scripts" / "qa.py"
    done = subprocess.run([sys.executable, "-I", str(launcher), "--role", role, "--skill", skill] + list(args),
                          cwd=str(cwd or work), capture_output=True, text=True, timeout=300, env=env)
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        raise AssertionError("not JSON: %s %s" % (done.stdout[-500:], done.stderr[-500:]))


def project(tmp_path):
    work = tmp_path / "proj"
    work.mkdir()
    shutil.copytree(str(CAPTURE / "shipping"), str(work / "shipping"), ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(str(CAPTURE / "tests"), str(work / "tests"), ignore=shutil.ignore_patterns("__pycache__"))
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial import")
    shutil.copytree(str(SKILL), str(work / ".claude" / "skills" / "qa-mutation"),
                    ignore=shutil.ignore_patterns("evals", "__pycache__"))
    (work / ".git" / "info").mkdir(exist_ok=True)
    with (work / ".git" / "info" / "exclude").open("a") as handle:
        handle.write("\n/.claude/skills/\n")
    return work


def mutation_run(tmp_path):
    """The skill's mutation run in a suite worktree; returns ``(work, env, commit, run output)``."""
    work, env = project(tmp_path), environment(tmp_path)
    assert kernel(work, env, "init", role="user", skill="user")[0] == 0
    code, out = kernel(work, env, "config", "set", "project.trust", "trusted", "--approval", APPROVAL, role="user", skill="user")
    assert code == 0, out
    code, tree = kernel(work, env, "worktree", "add", "HEAD")
    assert code == 0, tree
    wt = work / tree["path"]
    shutil.copytree(str(CAPTURE / "captured" / "mutants"), str(wt / "mutants"))   # what mutmut run leaves in the worktree
    (wt / "setup.cfg").write_text("[mutmut]\nsource_paths = shipping/\npytest_add_cli_args_test_selection = tests/\n")
    code, run = kernel(work, env, "run", "--timeout", "300", "--report", "mutation:" + CONVERTED, "--shell", "--", SHELL, cwd=wt)
    assert code == 0 and run["status"] == "passed", run
    return work, env, tree["commit"], run


def stored_report(work, run):
    found = sorted((work / run["evidence"]).rglob("mutation-report.json"))
    assert found and found[0].parent.name == "reports", found
    return found[0]


def test_the_converted_report_is_the_mutation_runs_own_measurement_and_its_ingest_is_structured(tmp_path):
    work, env, commit, run = mutation_run(tmp_path)
    assert [m["kind"] for m in run["measurements"]] == ["mutants"]
    by_file = run["measurements"][0]["totals"]["by_file"]
    counts = {}
    for statuses in by_file.values():
        for status, number in statuses.items():
            counts[status] = counts.get(status, 0) + number
    assert counts == {"Killed": 15, "Survived": 4, "NoCoverage": 7}       # the capture, as tests/corpus/MANIFEST.json states it
    report = stored_report(work, run)
    code, ingested = kernel(work, env, "ingest", "mutation", str(report.relative_to(work)), "--from", run["execution"])
    assert code == 0 and ingested["strength"] == "structured", ingested
    # The same report converted outside the run is the user's file as far as the kernel can tell: attested, never a score.
    loose = work / ".qa" / "work" / "loose.json"
    loose.write_text(report.read_text().replace('"high": 80', '"high": 81'))
    code, other = kernel(work, env, "ingest", "mutation", str(loose.relative_to(work)), "--from", run["execution"])
    assert code == 0 and other["strength"] == "attested", other
    code, shown = kernel(work, env, "state", "show")
    record = shown["state"]["executions"][run["execution"]]
    assert "mutmut run" in " ".join(record["command"]["argv"]) and record["git"]["worktree"].startswith(".qa/work/")
    assert [i["strength"] for i in record["ingested"]] == ["structured", "attested"]


@pytest.mark.parametrize("min_score,outcome", [(55, "MET"), (60, "NOT_MET")])     # the capture scores 15 / 26 = 57.7
def test_a_mutation_score_min_gate_reads_the_structured_score(tmp_path, min_score, outcome):
    work, env, commit, run = mutation_run(tmp_path)
    report = stored_report(work, run)
    code, ingested = kernel(work, env, "ingest", "mutation", str(report.relative_to(work)), "--from", run["execution"])
    assert ingested["strength"] == "structured"
    override = json.dumps({"enabled": True, "params": {"paths": ["shipping/**"], "min_score": min_score}})
    code, proposed = kernel(work, env, "gate", "policy", "propose", "mutation_score_min", "--override", override,
                            "--rationale", "the shipping package must withstand mutation")
    assert code == 0, proposed
    code, accepted = kernel(work, env, "gate", "policy", "accept", proposed["event"], "--approval", APPROVAL, role="user", skill="user")
    assert code == 0, accepted
    code, gate = kernel(work, env, "gate", "evaluate", "--profile", "release", "--target", commit)
    criterion = next(c for c in gate["criteria"] if c["type"] == "mutation_score_min")
    assert criterion["outcome"] == outcome, criterion
    assert criterion["evidence"] == [run["execution"], ingested["event"]], criterion     # the structured ingest is what it read
    assert criterion["detail"].startswith("57.69 %s %d" % (">=" if outcome == "MET" else "<", min_score)), criterion


PIT = REPO / "tests" / "corpus" / "producers" / "gradle_pitest"


def test_pit_converted_inside_its_run_in_a_worktree_is_structured(tmp_path):
    """PIT in a suite worktree on the host, the conversion in the same run (references/tools.md). A committed stub
    ``gradlew`` stands in for Gradle and PIT, which this host need not have: it leaves the captured mutations.xml where
    the Gradle plugin writes it."""
    work = tmp_path / "proj"
    work.mkdir()
    shutil.copytree(str(PIT / "src"), str(work / "src"))
    gradlew = work / "gradlew"
    gradlew.write_text("#!/bin/sh\nmkdir -p build/reports/pitest\ncp '%s' build/reports/pitest/mutations.xml\n"
                       % (PIT / "captured" / "mutations.xml"))
    gradlew.chmod(gradlew.stat().st_mode | stat.S_IXUSR)
    (work / ".gitignore").write_text("/build/\n")
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial import")
    shutil.copytree(str(SKILL), str(work / ".claude" / "skills" / "qa-mutation"),
                    ignore=shutil.ignore_patterns("evals", "__pycache__"))
    with (work / ".git" / "info" / "exclude").open("a") as handle:
        handle.write("\n/.claude/skills/\n")
    env = environment(tmp_path)
    kernel(work, env, "init", role="user", skill="user")
    kernel(work, env, "config", "set", "project.trust", "trusted", "--approval", APPROVAL, role="user", skill="user")
    code, tree = kernel(work, env, "worktree", "add", "HEAD")
    assert code == 0, tree
    shell = ("./gradlew pitest --offline && python3 ../../../.claude/skills/qa-mutation/scripts/pit_to_report.py"
             " --xml build/reports/pitest/mutations.xml --out " + CONVERTED + " --source-root src/main/java")
    code, run = kernel(work, env, "run", "--report", "mutation:" + CONVERTED, "--shell", "--", shell, cwd=work / tree["path"])
    assert code == 0 and run["status"] == "passed", run
    counts = {}
    for statuses in run["measurements"][0]["totals"]["by_file"].values():
        for status, number in statuses.items():
            counts[status] = counts.get(status, 0) + number
    assert counts == {"Killed": 9, "Survived": 1, "NoCoverage": 3}      # PIT's statistics block, tests/corpus/MANIFEST.json
    code, ingested = kernel(work, env, "ingest", "mutation", str(stored_report(work, run).relative_to(work)),
                            "--from", run["execution"])
    assert code == 0 and ingested["strength"] == "structured", ingested
    assert not (work / "build").exists()                                 # the user's checkout is not written


def test_a_file_the_run_does_not_declare_is_not_kept_in_its_evidence(tmp_path):
    """Why a container run cannot hand PIT's XML to a later conversion: the kernel has no format for that XML, and a
    file in the evidence directory that no declared report names is removed when the run is recorded."""
    work, env = project(tmp_path), environment(tmp_path)
    kernel(work, env, "init", role="user", skill="user")
    kernel(work, env, "config", "set", "project.trust", "trusted", "--approval", APPROVAL, role="user", skill="user")
    code, run = kernel(work, env, "run", "--shell", "--", "touch {evidence}/mutations.xml")
    assert run["execution"] and (work / run["evidence"] / "stdout.log").is_file(), run
    assert not (work / run["evidence"] / "mutations.xml").exists()


def test_a_conversion_run_of_its_own_would_give_a_gate_score_no_mutation_run_produced(tmp_path):
    """Why the conversion is not a kernel run of its own after ``mutmut run``: such a run records the converted file as its
    own report, so the gate reads it as a tool's score, yet the run's command ran no mutation tool and ``mutants/`` is
    unrecorded worktree state. Here no mutation tool runs at all (the results are only placed in the worktree) and the gate
    still scores. In the skill's form, mutmut and the converter share one recorded run."""
    work, env = project(tmp_path), environment(tmp_path)
    kernel(work, env, "init", role="user", skill="user")
    kernel(work, env, "config", "set", "project.trust", "trusted", "--approval", APPROVAL, role="user", skill="user")
    code, tree = kernel(work, env, "worktree", "add", "HEAD")
    wt = work / tree["path"]
    shutil.copytree(str(CAPTURE / "captured" / "mutants"), str(wt / "mutants"))
    (wt / "setup.cfg").write_text("[mutmut]\nsource_paths = shipping/\n")
    code, run = kernel(work, env, "run", "--report", "mutation:" + CONVERTED, "--", "python3",
                       "../../../.claude/skills/qa-mutation/scripts/mutmut_to_report.py", "--root", ".", "--out", CONVERTED, cwd=wt)
    assert run["status"] == "passed" and [m["kind"] for m in run["measurements"]] == ["mutants"], run
    code, shown = kernel(work, env, "state", "show")
    assert not any("mutmut run" in " ".join(r["command"]["argv"]) for r in shown["state"]["executions"].values())
    override = json.dumps({"enabled": True, "params": {"paths": ["shipping/**"], "min_score": 55}})
    code, proposed = kernel(work, env, "gate", "policy", "propose", "mutation_score_min", "--override", override, "--rationale", "r")
    kernel(work, env, "gate", "policy", "accept", proposed["event"], "--approval", APPROVAL, role="user", skill="user")
    code, gate = kernel(work, env, "gate", "evaluate", "--profile", "release", "--target", tree["commit"])
    criterion = next(c for c in gate["criteria"] if c["type"] == "mutation_score_min")
    assert criterion["outcome"] == "MET" and criterion["evidence"] == [run["execution"]], criterion


def test_the_earlier_flow_converting_outside_the_run_gives_an_attested_ingest_the_gate_ignores(tmp_path):
    """Before the ruling the skill ran ``mutmut run`` alone and converted afterwards, outside any run: the kernel cannot tell
    that file from one a person wrote, so the gate never reads it. This is the case the flow above replaces."""
    work, env = project(tmp_path), environment(tmp_path)
    kernel(work, env, "init", role="user", skill="user")
    kernel(work, env, "config", "set", "project.trust", "trusted", "--approval", APPROVAL, role="user", skill="user")
    code, tree = kernel(work, env, "worktree", "add", "HEAD")
    wt = work / tree["path"]
    shutil.copytree(str(CAPTURE / "captured" / "mutants"), str(wt / "mutants"))
    code, run = kernel(work, env, "run", "--", "mutmut", "run", cwd=wt)
    assert run["status"] == "passed", run
    converter = work / ".claude" / "skills" / "qa-mutation" / "scripts" / "mutmut_to_report.py"
    done = subprocess.run([sys.executable, "-I", str(converter), "--root", str(wt), "--out", str(work / ".qa" / "work" / "m.json")],
                          capture_output=True, text=True, timeout=120, env=env)
    assert done.returncode == 0, done.stderr
    code, ingested = kernel(work, env, "ingest", "mutation", ".qa/work/m.json", "--from", run["execution"])
    assert ingested["strength"] == "attested", ingested
    override = json.dumps({"enabled": True, "params": {"paths": ["shipping/**"], "min_score": 50}})
    code, proposed = kernel(work, env, "gate", "policy", "propose", "mutation_score_min", "--override", override, "--rationale", "r")
    kernel(work, env, "gate", "policy", "accept", proposed["event"], "--approval", APPROVAL, role="user", skill="user")
    code, gate = kernel(work, env, "gate", "evaluate", "--profile", "release", "--target", tree["commit"])
    criterion = next(c for c in gate["criteria"] if c["type"] == "mutation_score_min")
    assert criterion["outcome"] == "NO_EVIDENCE" and "attested" in criterion["detail"], criterion
