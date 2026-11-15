"""qa ci begin and qa ci generate (§12.2 "Unattended CI", §22.2 #10).

The workspaces here are git work trees built with fixed contents, identities and dates, so the commit, the configuration
hash and so the generated workflow are the same on every machine: tests/corpus/ci/qa-reexecution.yml is that workflow.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import test_compare
from helpers import CORPUS, LAUNCHER as KERNEL_LAUNCHER
from qa import authz, bundle, config, execution, gate, ids, ledger, state, workspace
from qa.errors import QaError

ACTOR = ledger.Actor(role="verifier", skill="qa-release", session="S-" + "0" * 26, independence="I1")
RUNNER = ledger.Actor(role="verifier", skill="qa-release", session="S-" + "1" * 26, independence="I1")
FIXED_EXE = "EXE-" + "0" * 25 + "1"
LAUNCHER = ".claude/skills/qa-release/scripts/qa.py"
CI_ENV = "ci-github"
ARGV = ("python3", "tests/run_junit.py", "--out", "{evidence}/junit.xml")
REPORTS = (("junit", "{evidence}/junit.xml"),)
OTHER_HASH = "sha256:" + "1" * 64
EXPECTED = CORPUS / "ci" / "qa-reexecution.yml"
ENVIRON = {"PATH": os.environ.get("PATH", os.defpath)}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}
RUN_JUNIT = ("import pathlib\n"
             "import sys\n"
             "\n"
             "# Writes a one-case JUnit report to the path after --out; every other argument is ignored.\n"
             "args = sys.argv[1:]\n"
             "if \"--out\" in args:\n"
             "    pathlib.Path(args[args.index(\"--out\") + 1]).write_text(\n"
             "        '<testsuite tests=\"1\"><testcase classname=\"fixture\" name=\"passes\"/></testsuite>')\n")
LAUNCHER_TEXT = "# Stands for the launcher a project-scoped install tracks; the tests never run it.\n"


# --------------------------------------------------------------------------- building projects

def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
                           "-c", "core.hooksPath=" + os.devnull, *args],
                          env=dict(os.environ, **GIT_ENV), check=True, capture_output=True)


def head(root):
    return git(root, "rev-parse", "HEAD").stdout.decode("ascii").strip()


def ci_config():
    """The default configuration with a trusted project, a ci environment and its standing authorization."""
    cfg = workspace.default_config("ci-fixture")
    cfg["project"]["trust"] = "trusted"
    cfg["environments"].append({"id": CI_ENV, "class": "ci"})
    cfg["standing_authorizations"].append({"environment": CI_ENV, "activities": ["test-execution-local"]})
    return cfg


def build_tree(root, cfg=None):
    """``root`` as a CI checkout finds it: one commit holding the project, the launcher and the two shareable .qa/ files."""
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    files = {"tests/run_junit.py": RUN_JUNIT.encode("utf-8"), LAUNCHER: LAUNCHER_TEXT.encode("utf-8"),
             ".qa/.gitignore": workspace.GITIGNORE.encode("utf-8"),
             ".qa/config.json": workspace.config_bytes(ci_config() if cfg is None else cfg)}
    for rel, data in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(str(path), 0o644)
    git(root, "init", "-q")
    git(root, "add", "-f", "--", *files)
    git(root, "commit", "-q", "-m", "fixture")
    return Path(root)


def build_project(root, actor=ACTOR):
    """A developer's workspace on ``build_tree``'s commit, with the configuration accepted."""
    build_tree(root)
    ws = workspace.init_workspace(root)[0]
    config.record_initialized(ws, actor)
    return ws


@contextlib.contextmanager
def fixed_execution_id(exe_id):
    real = ids.new_id
    if exe_id is not None:
        ids.new_id = lambda prefix: exe_id if prefix == "EXE" else real(prefix)
    try:
        yield
    finally:
        ids.new_id = real


def run_in(ws, env="local", argv=ARGV, actor=ACTOR, exe_id=None, reports=REPORTS, environ=None, **kw):
    request = execution.RunRequest(command=list(argv), reports=reports, environment=env, **kw)
    with fixed_execution_id(exe_id):
        return execution.execute(ws, actor, request, environ=ENVIRON if environ is None else environ)


def record_host_run(ws, argv=ARGV, **kw):
    return run_in(ws, argv=argv, **kw)["execution"]


def config_hash_of(root):
    return "sha256:" + hashlib.sha256((Path(root) / ".qa" / "config.json").read_bytes()).hexdigest()


def begin(root, config_hash, environ=None, actor=RUNNER):
    from qa import ci

    return ci.begin(Path(root), actor, config_hash=config_hash, environ={} if environ is None else environ)


def generate(ws, executions, **kw):
    from qa import ci

    options = dict(platform="github-actions", environment=CI_ENV, launcher=LAUNCHER)
    options.update(kw)
    return ci.generate(ws, ACTOR, executions=executions, **options)


def workflow_of(ws, generated):
    """The file the patch creates, read back from its added lines."""
    lines = (ws.root / generated["patch"]).read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("@@ ")) + 1
    assert all(line.startswith("+") for line in lines[start:])
    return "".join(line[1:] for line in lines[start:])


def events_of(ws, event_type):
    return [e for e in ledger.iter_all_events(ws) if e["type"] == event_type]


def synthetic_run(ws, actor, number, **over):
    """A schema-valid execution.recorded written straight to the ledger; ``over`` replaces whole fields."""
    data = dict(test_compare._execution("EXE-%026d" % number), **over)
    ledger.append(ws, actor, "execution.recorded", data)
    return data["id"]


def clean_git(root):
    return {"commit": head(root), "branch": None, "dirty": False, "diff_hash": "sha256:" + "0" * 64}


@pytest.fixture
def fresh_repo(tmp_path):
    return build_tree(tmp_path / "checkout")


@pytest.fixture
def pinned(fresh_repo):
    assert begin(fresh_repo, config_hash_of(fresh_repo))["matched"] is True
    return workspace.init_workspace(fresh_repo)[0]


@pytest.fixture
def ws_with_run(tmp_path):
    ws = build_project(tmp_path / "project")
    assert record_host_run(ws, exe_id=FIXED_EXE) == FIXED_EXE
    assert state.load(ws)["executions"][FIXED_EXE]["status"] == "passed"
    return ws


@pytest.fixture
def ci_ws(ws, actor):
    """The ws fixture's workspace made a git work tree with the CI configuration accepted."""
    build_tree(ws.root)
    config.record_initialized(ws, actor)
    return ws


# --------------------------------------------------------------------------- qa ci begin

def test_ci_begin_pins_a_fresh_checkout_to_the_accepted_config(fresh_repo):
    expected = config_hash_of(fresh_repo)
    out = begin(fresh_repo, expected, environ={"GITHUB_RUN_ID": "17", "GITHUB_SHA": head(fresh_repo)})
    assert (out["expected"], out["found"], out["matched"]) == (expected, expected, True)
    assert out["runner"] == {"GITHUB_RUN_ID": "17", "GITHUB_SHA": head(fresh_repo)}
    ws = workspace.init_workspace(fresh_repo)[0]
    [accepted] = events_of(ws, "config.accepted")
    assert {k: v for k, v in accepted["data"].items() if k != "authority_mac"} == {
        "hash": expected, "approval": "ci-pin " + expected, "basis": "ci-pin"}
    st = state.load(ws)
    assert config.accepted_hash(st) == expected and st["config"]["accepted_basis"] == "ci-pin" and config.ci_pinned(st)
    rec = run_in(ws, env=CI_ENV, actor=RUNNER)
    assert (rec["status"], rec["reason"]) == ("passed", None)
    assert state.load(ws)["executions"][rec["execution"]]["authorization"] == "standing:" + CI_ENV


def test_ci_begin_on_changed_config_blocks_every_run(fresh_repo):
    out = begin(fresh_repo, config_hash=OTHER_HASH)
    assert (out["matched"], out["found"]) == (False, config_hash_of(fresh_repo))
    ws = workspace.init_workspace(fresh_repo)[0]
    assert [e["type"] for e in ledger.iter_all_events(ws)] == ["ci.begun"]
    rec = run_in(ws, env=CI_ENV, actor=RUNNER)
    assert (rec["status"], rec["reason"]) == ("blocked", "config_drift")
    rec = run_in(ws, env="local", actor=RUNNER)
    assert (rec["status"], rec["reason"]) == ("blocked", "config_drift")
    st = state.load(ws)
    assert config.ci_pinned(st) and config.accepted_hash(st) is None
    static = authz.check(st, config.load_config(ws), activity="static-analysis", environment={"id": CI_ENV, "class": "ci"},
                         now=ids.now_utc())
    assert static.allowed


def test_ci_begin_after_workspace_initialized_with_a_changed_config_still_accepts_nothing(fresh_repo):
    ws = workspace.init_workspace(fresh_repo)[0]
    config.record_initialized(ws, RUNNER)  # the one event a fresh workspace may hold; it accepted the file as it stood
    assert begin(fresh_repo, config_hash=OTHER_HASH)["matched"] is False
    st = state.load(ws)
    assert config.ci_pinned(st) and config.accepted_hash(st) is None
    rec = run_in(ws, env=CI_ENV, actor=RUNNER)
    assert (rec["status"], rec["reason"]) == ("blocked", "config_drift")


def test_ci_begin_refuses_a_workspace_with_events(ws_with_run):
    before = ledger.iter_all_events(ws_with_run)
    with pytest.raises(QaError) as e:
        begin(ws_with_run.root, config_hash_of(ws_with_run.root))
    assert e.value.code == "ci_workspace_not_fresh"
    assert ledger.iter_all_events(ws_with_run) == before


def test_ci_begin_refuses_a_ledger_line_it_cannot_read(fresh_repo):
    ws = workspace.init_workspace(fresh_repo)[0]
    ws.path("ledger").mkdir(exist_ok=True)
    ws.path("ledger", RUNNER.session + ".jsonl").write_bytes(b"not an event\n")
    with pytest.raises(QaError) as e:
        begin(fresh_repo, config_hash_of(fresh_repo))
    assert e.value.code == "ci_workspace_not_fresh"


@pytest.mark.parametrize("bad", ["sha256:" + "A" * 64, "sha256:" + "0" * 63, "0" * 64, ""])
def test_ci_begin_refuses_a_malformed_config_hash(fresh_repo, bad):
    with pytest.raises(QaError) as e:
        begin(fresh_repo, bad)
    assert e.value.code == "invalid_config_hash"


def test_runner_fields_keep_only_allowlisted_well_formed_values():
    from qa import ci

    environ = {"GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REPOSITORY": "owner/repo",
               "GITHUB_SHA": "a" * 40, "GITHUB_WORKFLOW_REF": "owner/repo/.github/workflows/qa-reexecution.yml@refs/heads/main",
               "GITHUB_TOKEN": "not-a-real-token-value", "HOME": "/home/runner", "GITHUB_REF_NAME": "main", "CI": "true"}
    assert ci.runner_fields(environ) == {name: environ[name] for name in ci.RUNNER_FIELDS}
    assert ci.RUNNER_FIELDS == ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_WORKFLOW_REF")
    for bad in ("", "a b", "x\n", "${{ github.token }}", "a" * 201, "café", "a;b", "a\"b"):
        assert ci.runner_fields({"GITHUB_RUN_ID": bad, "GITHUB_SHA": "a" * 40}) == {"GITHUB_SHA": "a" * 40}
    assert ci.runner_fields({"GITHUB_RUN_ID": "a" * 200}) == {"GITHUB_RUN_ID": "a" * 200}


def test_ci_begin_records_only_well_formed_runner_fields(fresh_repo):
    environ = {"GITHUB_RUN_ID": "99", "GITHUB_REPOSITORY": "bad value", "GITHUB_TOKEN": "not-a-real-token-value"}
    begin(fresh_repo, config_hash_of(fresh_repo), environ=environ)
    [begun] = events_of(workspace.init_workspace(fresh_repo)[0], "ci.begun")
    assert begun["data"]["runner"] == {"GITHUB_RUN_ID": "99"}


# --------------------------------------------------------------------------- the CI-pinned workspace

def test_ci_environment_outside_a_pinned_workspace_is_policy_denied(ws, actor):
    ws.path("config.json").write_bytes(workspace.config_bytes(ci_config()))
    config.record_initialized(ws, actor)
    rec = run_in(ws, env=CI_ENV, actor=actor)
    assert (rec["status"], rec["reason"]) == ("blocked", "policy_denied")
    d = authz.check(state.load(ws), config.load_config(ws), activity="static-analysis",
                    environment={"id": CI_ENV, "class": "ci"}, now=ids.now_utc())
    assert (d.allowed, d.reason) == (False, "policy_denied") and "qa ci begin" in d.detail


def test_ci_true_on_a_workstation_grants_nothing(ws, actor, monkeypatch):
    ws.path("config.json").write_bytes(workspace.config_bytes(ci_config()))
    config.record_initialized(ws, actor)
    cfg, env = config.load_config(ws), {"id": CI_ENV, "class": "ci"}

    def decisions():
        st = state.load(ws)
        return [authz.check(st, cfg, activity=a, environment=e, now=ids.now_utc())
                for a in ("static-analysis", "test-execution-local", "db-write") for e in (env, cfg["environments"][0])]

    before = decisions()
    for name, value in (("CI", "true"), ("GITHUB_ACTIONS", "true"), ("GITHUB_RUN_ID", "1"), ("GITHUB_SHA", "a" * 40)):
        monkeypatch.setenv(name, value)
    assert decisions() == before
    rec = run_in(ws, env=CI_ENV, actor=actor, environ=dict(os.environ))
    assert (rec["status"], rec["reason"]) == ("blocked", "policy_denied")
    assert not config.ci_pinned(state.load(ws))
    with pytest.raises(QaError) as e:
        authz.grant(ws, actor, environment_id=CI_ENV, targets=[], activities=["test-execution-local"], limits={},
                    approval="ok", expires_at=None)
    assert e.value.code == "ci_unsupported" and "standing authorizations from config.json only" in e.value.message


def test_ci_pinned_workspace_refuses_grants_and_verdicts(pinned):
    with pytest.raises(QaError) as e:
        authz.grant(pinned, RUNNER, environment_id="local", targets=[], activities=["db-ephemeral"], limits={},
                    approval="ok", expires_at=None)
    assert e.value.code == "ci_no_grants"
    with pytest.raises(QaError) as e:
        gate.evaluate(pinned, RUNNER, profile="change")
    assert e.value.code == "ci_no_verdict"
    for change in (lambda: config.accept(pinned, RUNNER, "ok"),
                   lambda: config.set_value(pinned, RUNNER, "project.trust", "untrusted", "ok"),
                   lambda: gate.accept(pinned, RUNNER, "EVT-X", "ok")):
        with pytest.raises(QaError) as e:
            change()
        assert e.value.code == "ci_config_pinned"
    assert [e["type"] for e in ledger.iter_all_events(pinned)] == ["ci.begun", "config.accepted"]


def test_ci_pinned_workspace_denies_activities_outside_the_runner_set(pinned):
    st, cfg = state.load(pinned), config.load_config(pinned)
    local, ci_env = cfg["environments"][0], {"id": CI_ENV, "class": "ci"}
    for activity in ("db-write", "load-test", "dast-passive", "db-readonly", "write-tests", "data-egress"):
        for env in (local, ci_env):
            d = authz.check(st, cfg, activity=activity, environment=env, now=ids.now_utc())
            assert (d.allowed, d.reason) == (False, "policy_denied"), (activity, env["id"])
    allowed = authz.check(st, cfg, activity="test-execution-local", environment=ci_env, now=ids.now_utc())
    assert (allowed.allowed, allowed.authorization) == (True, "standing:" + CI_ENV)
    untrusted = dict(cfg, project=dict(cfg["project"], trust="untrusted"))
    d = authz.check(st, untrusted, activity="test-execution-local", environment=ci_env, now=ids.now_utc())
    assert (d.allowed, d.reason) == (False, "not_authorized")  # the trust rule still applies
    rec = run_in(pinned, env="local", actor=RUNNER, activity="load-test")
    assert (rec["status"], rec["reason"]) == ("blocked", "policy_denied")


def test_ci_pinned_workspace_runs_db_ephemeral_only_on_a_database_the_suite_created(pinned):
    """§12.2: db-ephemeral is runner-confined only for a database the suite creates; nothing in 4b records one."""
    rec = run_in(pinned, env=CI_ENV, actor=RUNNER, activity="db-ephemeral")
    assert (rec["status"], rec["reason"]) == ("blocked", "policy_denied")
    assert any("db-ephemeral" in note and "the suite created" in note for note in rec["limitations"])


# --------------------------------------------------------------------------- qa ci generate

def test_generated_workflow_matches_the_expected_file(ws_with_run):
    out = generate(ws_with_run, [FIXED_EXE])
    assert workflow_of(ws_with_run, out).encode("utf-8") == EXPECTED.read_bytes()


def test_generated_workflow_pins_actions_and_drops_credentials(ws_with_run):
    from qa import resource_dir

    out = generate(ws_with_run, [FIXED_EXE])
    text = workflow_of(ws_with_run, out)
    policy = json.loads((resource_dir("policies") / "ci-actions.json").read_text(encoding="utf-8"))
    pinned = {(a["uses"], a["sha"], a["tag"]) for a in policy["actions"]}
    uses = re.findall(r"^\s+uses: (\S+)@(\S+) # (\S+)$", text, flags=re.M)
    assert len(uses) == 2 and set(uses) == pinned and all(re.fullmatch(r"[0-9a-f]{40}", sha) for _, sha, _ in uses)
    assert out["actions"] == policy["actions"]
    assert text.count("uses:") == 2
    assert "persist-credentials: false" in text and "\npermissions: {}\n" in text and "contents: read" in text
    commit = state.load(ws_with_run)["executions"][FIXED_EXE]["git"]["commit"]
    assert ("ref: %s\n" % commit) in text and out["target_commit"] == commit
    for word in ("secrets", "continue-on-error", "pull_request", "GITHUB_TOKEN", "github.event", "write"):
        assert word not in text
    assert set(re.findall(r"\$\{\{[^}]*\}\}", text)) == {"${{ !cancelled() }}", "${{ runner.temp }}"}
    blocks = run_blocks(text)
    assert blocks and not any("${{" in block for block in blocks)
    assert blocks[0] == "python3 %s ci begin --config-hash %s" % (LAUNCHER, config_hash_of(ws_with_run.root))
    assert blocks[-1] == 'python3 %s report bundle --output "$RUNNER_TEMP/qa-evidence-bundle.tar.gz"' % LAUNCHER
    assert blocks[1] == ("python3 %s run --environment %s --scope targeted --report 'junit:{evidence}/junit.xml' -- "
                         "python3 tests/run_junit.py --out '{evidence}/junit.xml'" % (LAUNCHER, CI_ENV))


def run_blocks(text):
    """The text of every ``run: |`` block, its lines joined with newlines."""
    lines, blocks = text.splitlines(), []
    for i, line in enumerate(lines):
        if line.strip() == "run: |":
            indent = len(line) - len(line.lstrip()) + 2
            body = []
            for following in lines[i + 1:]:
                if following.strip() and len(following) - len(following.lstrip()) < indent:
                    break
                body.append(following[indent:])
            blocks.append("\n".join(body).rstrip("\n"))
    return blocks


def test_ci_generate_writes_only_a_patch(ws_with_run):
    def outside_qa():
        return sorted(p.relative_to(ws_with_run.root).as_posix() for p in ws_with_run.root.rglob("*")
                      if ".qa" not in p.relative_to(ws_with_run.root).parts and ".git" not in p.relative_to(ws_with_run.root).parts)

    before = outside_qa()
    out = generate(ws_with_run, [FIXED_EXE])
    assert outside_qa() == before and not (ws_with_run.root / ".github").exists()
    text = workflow_of(ws_with_run, out)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert out["sha256"] == "sha256:" + digest
    assert out["patch"] == ".qa/work/patches/qa-ci-%s.patch" % digest[:12]
    assert sorted(p.name for p in ws_with_run.path("work", "patches").iterdir()) == ["qa-ci-%s.patch" % digest[:12]]
    assert out["workflow_path"] == ".github/workflows/qa-reexecution.yml"
    [event] = events_of(ws_with_run, "ci.generated")
    assert event["data"] == out
    assert set(out) == {"patch", "sha256", "workflow_path", "platform", "environment", "target_commit", "config_hash",
                        "launcher", "executions", "skipped", "actions"}
    assert (out["platform"], out["environment"], out["launcher"], out["executions"], out["skipped"]) == (
        "github-actions", CI_ENV, LAUNCHER, [FIXED_EXE], [])
    assert out["config_hash"] == config_hash_of(ws_with_run.root)
    git(ws_with_run.root, "apply", "--check", str(ws_with_run.root / out["patch"]))


def test_ci_generate_skips_container_dirty_and_env_runs(ci_ws, actor):
    clean = clean_git(ci_ws.root)
    good = synthetic_run(ci_ws, actor, 1, git=clean)
    contained = synthetic_run(ci_ws, actor, 2, git=clean, isolation="container")
    dirty = synthetic_run(ci_ws, actor, 3, git=dict(clean, dirty=True))
    no_git = synthetic_run(ci_ws, actor, 4, git=None)
    env = synthetic_run(ci_ws, actor, 5, git=clean, env_values={"NODE_ENV": "test"})
    db = synthetic_run(ci_ws, actor, 6, git=clean, activity="db-write")
    install = synthetic_run(ci_ws, actor, 7, git=clean, activity="install-dependencies")
    downloads = synthetic_run(ci_ws, actor, 8, git=clean, authorizations=[
        {"activity": "test-execution-local", "environment": "local", "authorization": "trusted:local"},
        {"activity": "network-download", "environment": "local", "authorization": "AUTH-001"}])
    unknown = "EXE-" + "0" * 25 + "Z"
    out = generate(ci_ws, [contained, dirty, no_git, env, db, install, downloads, unknown, good, good])
    assert out["executions"] == [good]
    assert out["skipped"] == [
        {"execution": contained, "reason": "container_run"}, {"execution": dirty, "reason": "dirty_tree"},
        {"execution": no_git, "reason": "dirty_tree"}, {"execution": env, "reason": "environment_variables"},
        {"execution": db, "reason": "activity_not_confined"}, {"execution": install, "reason": "not_covered"},
        {"execution": downloads, "reason": "not_covered"}, {"execution": unknown, "reason": "unknown_execution"}]
    with pytest.raises(QaError) as e:
        generate(ci_ws, [contained, unknown], name="other")
    assert e.value.code == "ci_no_executions"
    assert e.value.details["skipped"] == [{"execution": contained, "reason": "container_run"},
                                          {"execution": unknown, "reason": "unknown_execution"}]


def test_ci_generate_refuses_untracked_launcher_or_config(ci_ws, actor):
    good = synthetic_run(ci_ws, actor, 1, git=clean_git(ci_ws.root))
    (ci_ws.root / "tools").mkdir()
    (ci_ws.root / "tools" / "qa.py").write_text(LAUNCHER_TEXT)  # in the working tree, never committed
    for launcher in ("tools/qa.py", "missing/qa.py", "../outside/qa.py", "tests", None):
        with pytest.raises(QaError) as e:
            generate(ci_ws, [good], launcher=launcher)
        assert e.value.code == "ci_launcher_untracked", launcher
    assert generate(ci_ws, [good], launcher=str(ci_ws.root / LAUNCHER))["launcher"] == LAUNCHER
    config.set_value(ci_ws, actor, "project.name", "renamed", "test approval")  # accepted here, never committed
    with pytest.raises(QaError) as e:
        generate(ci_ws, [good], name="other")
    assert e.value.code == "ci_config_untracked"
    assert not list(ci_ws.root.glob(".github"))


@pytest.mark.parametrize("bad", ["${{ github.event.issue.title }}", "a\nb", "a\rb", "a\x1bb", "a\x7fb"])
def test_ci_generate_refuses_argv_with_expressions_or_newlines(ws_with_run, bad):
    exe = record_host_run(ws_with_run, argv=("python3", "tests/run_junit.py", bad))
    with pytest.raises(QaError, match="ci_argv_unrepresentable"):
        generate(ws_with_run, executions=[exe])
    assert not list((ws_with_run.root / ".qa/work/patches").glob("qa-ci-*"))


@pytest.mark.parametrize("bad", ["a\u2028b", "a\u2029b", "a\x85b", "a\x9bb"])
def test_ci_generate_refuses_argv_with_other_line_breaks(ci_ws, actor, bad):
    exe = synthetic_run(ci_ws, actor, 1, git=clean_git(ci_ws.root), command={"argv": ["python3", bad], "shell": False, "cwd": "."})
    with pytest.raises(QaError) as e:
        generate(ci_ws, [exe])
    assert e.value.code == "ci_argv_unrepresentable" and bad not in json.dumps(e.value.to_json(), ensure_ascii=False)


def test_ci_generate_refuses_a_report_pattern_holding_an_expression(ci_ws, actor):
    exe = synthetic_run(ci_ws, actor, 1, git=clean_git(ci_ws.root), reports=[
        {"format": "junit", "pattern": "${{ github.head_ref }}.xml", "files": [], "error": None}])
    with pytest.raises(QaError) as e:
        generate(ci_ws, [exe])
    assert e.value.code == "ci_argv_unrepresentable"


def test_ci_generate_refusals(ci_ws, actor, tmp_path):
    good = synthetic_run(ci_ws, actor, 1, git=clean_git(ci_ws.root))
    cases = [(dict(platform="gitlab-ci"), "ci_platform_unsupported"), (dict(environment="local"), "ci_environment_invalid"),
             (dict(environment="nowhere"), "ci_environment_invalid"), (dict(name="Bad Name"), "invalid_name"),
             (dict(name="-x"), "invalid_name"), (dict(name="a" * 41), "invalid_name")]
    for options, code in cases:
        with pytest.raises(QaError) as e:
            generate(ci_ws, [good], **options)
        assert e.value.code == code, options
    with pytest.raises(QaError) as e:
        generate(ci_ws, [])
    assert e.value.code == "ci_no_executions"
    other = synthetic_run(ci_ws, actor, 2, git=dict(clean_git(ci_ws.root), commit="b" * 40))
    with pytest.raises(QaError) as e:
        generate(ci_ws, [good, other])
    assert e.value.code == "ci_mixed_commits"
    workflows = ci_ws.root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "taken.yml").write_text("name: taken\n")
    with pytest.raises(QaError) as e:
        generate(ci_ws, [good], name="taken")
    assert e.value.code == "ci_workflow_exists"
    generate(ci_ws, [good])
    with pytest.raises(QaError) as e:
        generate(ci_ws, [good])
    assert e.value.code == "ci_patch_exists"
    assert len(events_of(ci_ws, "ci.generated")) == 1
    cfg = json.loads(ci_ws.path("config.json").read_text())
    cfg["project"]["name"] = "edited by hand"
    ci_ws.path("config.json").write_bytes(workspace.config_bytes(cfg))
    with pytest.raises(QaError) as e:
        generate(ci_ws, [good], name="third")
    assert e.value.code == "config_drift"


def test_ci_generate_refuses_a_workspace_below_the_top_of_its_repository(tmp_path, actor):
    build_tree(tmp_path / "repo")
    ws = workspace.init_workspace(tmp_path / "repo" / "service")[0]
    config.record_initialized(ws, actor)
    exe = synthetic_run(ws, actor, 1, git=clean_git(tmp_path / "repo"))
    cfg = ci_config()
    ws.path("config.json").write_bytes(workspace.config_bytes(cfg))
    config.accept(ws, actor, "test approval")
    with pytest.raises(QaError) as e:
        generate(ws, [exe])
    assert e.value.code == "ci_root_not_top"


def test_ci_generate_carries_what_the_run_recorded(ci_ws, actor):
    exe = synthetic_run(ci_ws, actor, 1, git=clean_git(ci_ws.root), activity="static-analysis", category="security",
                        command={"argv": ["ruff check . | tee out.txt"], "shell": True, "cwd": "tests"},
                        authorizations=[{"activity": "static-analysis", "environment": "local",
                                         "authorization": "default:static-analysis"}])
    text = workflow_of(ci_ws, generate(ci_ws, [exe]))
    assert "        working-directory: \"tests\"\n" in text
    assert ("python3 ../%s run --environment %s --scope targeted --activity static-analysis --category security --shell -- "
            "'ruff check . | tee out.txt'" % (LAUNCHER, CI_ENV)) in run_blocks(text)


def test_ci_generate_carries_the_recorded_timeout(ws_with_run):
    exe = record_host_run(ws_with_run, timeout_s=42.5, allow_empty=True)
    text = workflow_of(ws_with_run, generate(ws_with_run, [exe]))
    assert ("--report 'junit:{evidence}/junit.xml' --timeout 43 --allow-empty -- python3 tests/run_junit.py") in text


# A token shape the shipped redaction patterns catch (github_token), built at runtime so no file holds one.
TOKEN_SHAPED = "gh" + "p_" + "a1B2c3D4e5" * 3 + "f6G7h8"
MARKER = re.compile(r"\[REDACTED:github_token:[0-9a-f]{8}\]")


def test_ci_generate_skips_records_that_never_ran(ws_with_run, actor):
    """A blocked record lists only the checks that were allowed, so it is no command to re-run; nor is a not_run one."""
    denied = run_in(ws_with_run, argv=("git", "push"))  # a command the guard denies
    unauthorized = run_in(ws_with_run, argv=ARGV + ("https://downloads.example.org/x",))  # a host no environment declares
    assert (denied["status"], denied["reason"]) == ("blocked", "policy_denied")
    assert (unauthorized["status"], unauthorized["reason"]) == ("blocked", "not_authorized")
    assert state.load(ws_with_run)["executions"][unauthorized["execution"]]["activity"] == "test-execution-local"
    not_run = synthetic_run(ws_with_run, actor, 2, git=clean_git(ws_with_run.root), status="not_run", reason="all_skipped")
    never = [denied["execution"], unauthorized["execution"], not_run]
    out = generate(ws_with_run, [FIXED_EXE] + never)
    assert out["executions"] == [FIXED_EXE]
    assert out["skipped"] == [{"execution": exe, "reason": "not_executed"} for exe in never]
    with pytest.raises(QaError) as e:
        generate(ws_with_run, never, name="other")
    assert e.value.code == "ci_no_executions"
    assert e.value.details["skipped"] == [{"execution": exe, "reason": "not_executed"} for exe in never]


def test_ci_generate_skips_a_host_run_whose_argument_was_redacted(ws_with_run):
    """The stored argv holds the marker, not the value that ran; re-run as stored, it would pair with a different command.

    The guard denies an argument a redaction pattern matches, so a host run that ran holds a marker for a value the
    redactor knows by other means: here, the value of a secret-named environment variable."""
    value = "fixture-value-2026"
    exe = record_host_run(ws_with_run, argv=ARGV + ("--label", value), environ=dict(ENVIRON, FIXTURE_TOKEN=value))
    record = state.load(ws_with_run)["executions"][exe]
    assert record["status"] == "passed" and value not in json.dumps(record)
    assert re.fullmatch(r"\[REDACTED:env_value:[0-9a-f]{8}\]", record["command"]["argv"][-1])
    out = generate(ws_with_run, [FIXED_EXE, exe])
    assert (out["executions"], out["skipped"]) == ([FIXED_EXE], [{"execution": exe, "reason": "redacted"}])


@pytest.mark.parametrize("field", ["argv", "report", "cwd"])
def test_ci_generate_skips_a_record_holding_redacted_text(ci_ws, actor, field):
    over = {"argv": dict(command={"argv": ["python3", "run.py", "--label=" + TOKEN_SHAPED], "shell": False, "cwd": "."}),
            "report": dict(reports=[{"format": "junit", "pattern": "out/%s.xml" % TOKEN_SHAPED, "files": [], "error": None}]),
            "cwd": dict(command={"argv": ["python3", "run.py"], "shell": False, "cwd": "build/" + TOKEN_SHAPED})}[field]
    clean = clean_git(ci_ws.root)
    good = synthetic_run(ci_ws, actor, 1, git=clean)
    redacted = synthetic_run(ci_ws, actor, 2, git=clean, **over)
    stored = json.dumps(state.load(ci_ws)["executions"][redacted])
    assert TOKEN_SHAPED not in stored and MARKER.search(stored)
    out = generate(ci_ws, [good, redacted])
    assert (out["executions"], out["skipped"]) == ([good], [{"execution": redacted, "reason": "redacted"}])


def test_generated_workflow_sizes_the_job_limit_from_its_steps(ws_with_run):
    text = workflow_of(ws_with_run, generate(ws_with_run, [FIXED_EXE]))
    assert "\n    timeout-minutes: 20\n" in text  # ceil((900 s, the configuration's default, + 300 s) / 60)
    first = record_host_run(ws_with_run, timeout_s=60)
    second = record_host_run(ws_with_run, timeout_s=120)
    text = workflow_of(ws_with_run, generate(ws_with_run, [first, second], name="two"))
    assert "\n    timeout-minutes: 8\n" in text  # ceil((60 + 120 + 300) / 60)
    at_limit = record_host_run(ws_with_run, timeout_s=21300)
    text = workflow_of(ws_with_run, generate(ws_with_run, [at_limit], name="three"))
    assert "\n    timeout-minutes: 360\n" in text  # (21300 + 300) / 60, the GitHub-hosted job limit itself


def test_ci_generate_refuses_a_job_past_the_hosted_limit(ws_with_run):
    exe = record_host_run(ws_with_run, timeout_s=21600)
    with pytest.raises(QaError) as e:
        generate(ws_with_run, [FIXED_EXE, exe])
    assert e.value.code == "ci_time_budget_exceeded"
    assert (e.value.details["timeout_minutes"], e.value.details["limit_minutes"]) == (380, 360)  # ceil((900+21600+300)/60)
    assert not list(ws_with_run.root.glob(".qa/work/patches/qa-ci-*")) and not events_of(ws_with_run, "ci.generated")


def test_ci_generate_skips_db_ephemeral_runs_as_not_confined(tmp_path, actor):
    """The ci environment's standing authorization may list db-ephemeral, but a ci environment holds no suite-created database."""
    cfg = ci_config()
    cfg["standing_authorizations"][-1]["activities"].append("db-ephemeral")
    root = build_tree(tmp_path / "project", cfg)
    ws = workspace.init_workspace(root)[0]
    config.record_initialized(ws, actor)
    clean = clean_git(root)
    good = synthetic_run(ws, actor, 1, git=clean)
    db = synthetic_run(ws, actor, 2, git=clean, activity="db-ephemeral")
    via = synthetic_run(ws, actor, 3, git=clean, authorizations=[
        {"activity": "db-ephemeral", "environment": "local", "authorization": "standing:local"}])
    out = generate(ws, [good, db, via])
    assert out["executions"] == [good]
    assert out["skipped"] == [{"execution": db, "reason": "activity_not_confined"},
                              {"execution": via, "reason": "activity_not_confined"}]


def test_generated_workflow_runs_on_a_fresh_checkout(ws_with_run, tmp_path):
    """The job's steps, as written, on a clone of the target commit: the kernel's own launcher stands in for the tracked one."""
    out = generate(ws_with_run, [FIXED_EXE])
    runner = tmp_path / "runner"
    git(tmp_path, "clone", "-q", str(ws_with_run.root), str(runner))
    git(runner, "checkout", "-q", out["target_commit"])
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    env = dict(os.environ, RUNNER_TEMP=str(temp), GITHUB_RUN_ID="5", QA_SESSION="S-" + "2" * 26)
    for block in run_blocks(workflow_of(ws_with_run, out)):
        assert block.startswith("python3 %s " % LAUNCHER)
        script = '"$QA_PYTHON" "$QA_LAUNCHER" ' + block[len("python3 %s " % LAUNCHER):]
        done = subprocess.run(["sh", "-c", script], cwd=str(runner), capture_output=True, timeout=120,
                              env=dict(env, QA_PYTHON=sys.executable, QA_LAUNCHER=str(KERNEL_LAUNCHER)))
        assert done.returncode == 0, (block, done.stdout, done.stderr)
    remote = workspace.init_workspace(runner)[0]
    st = state.load(remote)
    assert config.ci_pinned(st)
    [run] = st["executions"].values()
    assert (run["status"], run["environment_id"], run["authorization"]) == ("passed", CI_ENV, "standing:" + CI_ENV)
    assert (temp / "qa-evidence-bundle.tar.gz").is_file()


def test_bundle_from_a_pinned_workspace_pairs_with_the_local_run(tmp_path):
    local = build_project(tmp_path / "local")
    local_exe = record_host_run(local)
    runner = tmp_path / "runner"
    git(tmp_path, "clone", "-q", str(local.root), str(runner))
    assert begin(runner, config_hash_of(local.root))["matched"] is True
    remote = workspace.init_workspace(runner)[0]
    remote_exe = run_in(remote, env=CI_ENV, actor=RUNNER)["execution"]
    made = bundle.create(remote, RUNNER, tmp_path / "bundle.tar.gz")
    assert made["executions"] == 1
    imported = bundle.ingest(local, ACTOR, tmp_path / "bundle.tar.gz")
    assert imported["comparisons"] == [{"local": local_exe, "remote": remote_exe, "commit": head(local.root), "matching": True}]
    [compared] = events_of(local, "bundle.compared")
    assert compared["data"]["matching"] is True
    assert not config.ci_pinned(state.load(local))  # the importer folds the job's ci.begun apart, so it keeps its verdict


# --------------------------------------------------------------------------- the CLI

def test_ci_begin_cli_exits_1_when_the_config_changed(fresh_repo, qa_cli):
    code, out = qa_cli("ci", "begin", "--config-hash", OTHER_HASH, workspace=fresh_repo)
    assert (code, out["ok"], out["matched"], out["reason"]) == (1, False, False, "ci_config_mismatch")


def test_ci_cli_pins_then_generates(fresh_repo, qa_cli, tmp_path):
    code, out = qa_cli("ci", "begin", "--config-hash", config_hash_of(fresh_repo), workspace=fresh_repo)
    assert (code, out["ok"], out["matched"]) == (0, True, True)
    code, out = qa_cli("run", "--environment", CI_ENV, "--report", "junit:{evidence}/junit.xml", "--", *ARGV,
                       cwd=fresh_repo, workspace=fresh_repo)
    assert (code, out["status"]) == (0, "passed")
    project = build_project(tmp_path / "project")
    exe = record_host_run(project)
    code, out = qa_cli("ci", "generate", "--platform", "github-actions", "--environment", CI_ENV, "--execution", exe,
                       "--launcher", LAUNCHER, workspace=project.root)
    assert (code, out["executions"], out["launcher"]) == (0, [exe], LAUNCHER)
    assert (project.root / out["patch"]).is_file()
