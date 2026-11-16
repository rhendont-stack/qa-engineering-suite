"""Tests for tools/run_plugin_eval.py, driven by a fake ``claude`` executable (no real agent, no network, no key).

Each test builds a throwaway repository holding a copy of the wrapper and of the harness it imports, plus two skills with
``claude plugin eval`` cases, so the wrapper's ``git archive`` export is of that repository and never of this one."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from helpers import REPO

WRAPPER = REPO / "tools" / "run_plugin_eval.py"
HARNESS = REPO / "tools" / "run_skill_eval.py"
SECRET = "sk-test-0123456789-plugin-eval-not-a-real-key"
# Names the platform adds to a child's environment on its own: Python sets LC_CTYPE, macOS sets __CF_USER_TEXT_ENCODING.
OS_ADDED = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
CLEAN = {"PATH", "HOME", "CLAUDE_CONFIG_DIR", "DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}
FORBIDDEN_FLAGS = ("--allow-tools", "--scaffold", "--publish-report", "--keep-temp", "--allow-real-servers")

# The fake CLI. Its environment is the wrapper's clean one, so it reads its behaviour (MODE, COST, PASS, EXIT, HELP_OMIT)
# from claude.cfg beside it and appends one JSON line per call to LOG. For ``plugin eval`` it records what it was given
# (the export's files, its manifest, the staged prompts) and writes a result shaped like the real CLI's: ``runsPerCase``
# comes from the case's ``runs:`` frontmatter, ``--runs`` limits the runs made, and ``casesPassed`` counts every case,
# as the real CLI does under ``--threshold 0``.
FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import base64, fnmatch, json, os, re, sys, time
    argv = sys.argv[1:]
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude.cfg")))
    mode = cfg.get("MODE", "ok")

    def log(entry):
        with open(cfg["LOG"], "a") as fh:
            fh.write(json.dumps(entry) + "\\n")

    def opt(name):
        return argv[argv.index(name) + 1] if name in argv else None

    if argv[:2] == ["plugin", "eval"] and "--help" in argv:
        log({"argv": argv, "env": dict(os.environ)})
        omit = cfg.get("HELP_OMIT", [])
        for flag in ("--ablation <mode>", "--case <glob>", "--eval-dir <dir>", "--json [path]", "--judge-model <model>",
                     "--max-cost-usd <usd>", "--model <model>", "--no-publish", "--output-dir <dir>", "--runs <n>",
                     "--threshold <0..1>", "--trust-plugin"):
            if flag.split()[0] not in omit:
                print("  " + flag)
        sys.exit(0)
    if argv[:2] != ["plugin", "eval"]:
        log({"argv": argv, "env": dict(os.environ)})
        sys.exit(64)
    root = argv[2]
    eval_dir = os.path.join(root, opt("--eval-dir"))
    files = sorted(os.path.relpath(os.path.join(d, f), root) for d, _, names in os.walk(root) for f in names)
    manifest = os.path.join(root, ".claude-plugin", "plugin.json")
    prompts = {}
    for name in sorted(os.listdir(eval_dir)):
        if os.path.isfile(os.path.join(eval_dir, name, "prompt.md")) and fnmatch.fnmatchcase(name, opt("--case") or "*"):
            prompts[name] = open(os.path.join(eval_dir, name, "prompt.md")).read()
    log({"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "files": files, "prompts": prompts,
         "manifest": open(manifest).read() if os.path.exists(manifest) else None})
    if mode == "hang":
        time.sleep(60)
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    runs = int(opt("--runs") or 3)
    cases = []
    for name, prompt in prompts.items():
        found = re.search(r"(?m)^runs: (\\d+)$", prompt)
        per_case = 3 if mode == "runs3" or not found else int(found.group(1))
        passed = cfg.get("PASS", {}).get(name, True)
        graders = [{"name": "01-loads-qa-demo", "passed": passed, "weight": 1, "withOnly": False, "scored": True,
                    "explanation": "Skill called %dx (expected 1..∞)" % (1 if passed else 0)}]
        with_arm = [{"score": 1 if passed else 0, "passed": passed, "turns": 2, "costUsd": 0.01, "judgeCostUsd": 0,
                     "durationSeconds": 3, "skippedPaidGraders": False, "graders": graders,
                     "error": cfg.get("RUN_ERROR")} for _ in range(min(runs, per_case))]
        cases.append({"name": name, "dir": os.path.join(opt("--eval-dir"), name), "runsPerCase": per_case,
                      "arms": {"with": with_arm}, "aggregates": {"score": 1 if passed else 0}})
    doc = {"schemaVersion": 1, "claudeVersion": "9.9.9", "durationSeconds": 12, "costUsd": float(cfg.get("COST", "0.42")),
           "partial": False, "suite": {"root": root, "plugins": [{"name": "qa-engineering-suite", "path": root}]},
           "cases": cases, "aggregates": {"casesTotal": len(cases), "casesPassed": len(cases)}}
    if mode == "nocost":
        del doc["costUsd"]
    if mode == "badcost":
        doc["costUsd"] = "0.42"
    report = "<html>report</html>"
    if mode == "leak":
        raw = key.encode()
        doc["note"] = "the key is " + key
        report = "<html>%s %s %s</html>" % (base64.b64encode(b"x" + raw).decode(), raw.hex(), raw.hex().upper())
        print("stdout has " + key)
        print("stderr has " + base64.urlsafe_b64encode(b"yz" + raw + b"!").decode(), file=sys.stderr)
    os.makedirs(opt("--output-dir"), exist_ok=True)
    if mode != "noresult":
        for path in (opt("--json"), os.path.join(opt("--output-dir"), "aggregate-result.json")):
            with open(path, "w") as fh:
                json.dump(doc, fh)
        with open(os.path.join(opt("--output-dir"), "report.html"), "w") as fh:
            fh.write(report)
        if mode == "leak":
            os.makedirs(os.path.join(opt("--output-dir"), "nested"), exist_ok=True)
            with open(os.path.join(opt("--output-dir"), "nested", "trace.jsonl"), "w") as fh:
                fh.write(json.dumps({"key": key}) + "\\n")
        print("Wrote " + opt("--json"))
    sys.exit(int(cfg.get("EXIT", 0)))
''')


def _case_files(skill, name, prompt, tools="[Skill, Read, Glob, Grep]"):
    return {
        "%s/prompt.md" % name: "---\ntags: [trigger, en]\nruns: 3\nmax_turns: 3\ntimeout_seconds: 180\nallowed_tools: %s\n---\n%s\n"
                               % (tools, prompt),
        "%s/case.yaml" % name: 'schema_version: "1.1"\nname: %s\ndescription: "d"\nexpected_outcome: "o"\n' % name,
        "%s/graders/01-loads-%s.md" % (name, skill): "---\ntype: tool_used\ntool: Skill\nmin: 1\narm: both\n---\n",
    }


def _git(repo, *args):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo.parent / "git-home"), "GIT_AUTHOR_NAME": "a",
           "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}
    done = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false"] + list(args), env=env, capture_output=True,
                          text=True, check=True)
    return done.stdout.strip()


def write(repo, files):
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def commit(s, files, message="change"):
    write(s.repo, files)
    _git(s.repo, "add", "-A")
    _git(s.repo, "commit", "-qm", message)
    return _git(s.repo, "rev-parse", "HEAD")


@pytest.fixture
def suite(tmp_path):
    """A throwaway repository with the wrapper, the harness, two skills with plugin eval cases and a fake CLI on PATH."""
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(str(WRAPPER), str(repo / "tools" / "run_plugin_eval.py"))
    shutil.copy(str(HARNESS), str(repo / "tools" / "run_skill_eval.py"))
    files = {".claude-plugin/marketplace.json": '{"name": "qa-engineering-suite", "plugins": []}\n',
             "skills/qa-demo/SKILL.md": "---\nname: qa-demo\ndescription: Demo.\n---\n\n# Demo\n",
             "skills/qa-demo/evals/harness-case.json": '{"id": "demo-1"}\n',
             "skills/qa-demo/evals/plugin/prompts.json": '{"skill": "qa-demo"}\n',
             "skills/qa-demo/evals/plugin/results/old/aggregate-result.json": "{}\n",
             "skills/qa-other/SKILL.md": "---\nname: qa-other\ndescription: Other.\n---\n",
             "skills/qa-other/evals/grade.py": "print('grader')\n"}
    for name, prompt in (("trigger-en-01", "Committed prompt one."), ("trigger-en-02", "Committed prompt two."),
                         ("near-en-01", "A near miss.")):
        files.update({"skills/qa-demo/evals/plugin/" + k: v for k, v in _case_files("qa-demo", name, prompt).items()})
    files.update({"skills/qa-other/evals/plugin/" + k: v for k, v in _case_files("qa-other", "trigger-en-01", "Other.").items()})
    write(repo, files)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
    exe.chmod(0o755)
    for name in ("home", "tmp"):
        (tmp_path / name).mkdir()

    class Suite:
        pass

    s = Suite()
    s.root, s.repo, s.bin, s.log = tmp_path, repo, bin_dir, tmp_path / "fake.log"
    s.wrapper = repo / "tools" / "run_plugin_eval.py"
    s.ledger = tmp_path / "ledger" / "m5a-costs.jsonl"
    s.home, s.tmp = tmp_path / "home", tmp_path / "tmp"
    s.base = _git(repo, "rev-parse", "HEAD")
    s.count = 0
    return s


def set_fake(s, **cfg):
    cfg.setdefault("LOG", str(s.log))
    (s.bin / "claude.cfg").write_text(json.dumps(cfg))


def start(s, *extra, budget="5", runs="1", case="trigger-*", out=None, env=None, skill="qa-demo", **cfg):
    """Starts the wrapper with a minimal caller environment; ``env`` adds (or, with None, removes) variables."""
    set_fake(s, **cfg)
    s.count += 1
    s.out = out or s.root / "out" / str(s.count)
    argv = [sys.executable, "-I", str(s.wrapper), "--skill", skill, "--runs", runs, "--model", "test-model",
            "--budget-usd", budget, "--ledger", str(s.ledger), "--out", str(s.out)] + (["--case", case] if case else []) \
        + list(extra)
    environ = {"PATH": str(s.bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(s.home),
               "TMPDIR": str(s.tmp), "ANTHROPIC_API_KEY": SECRET}
    for k, v in (env or {}).items():
        if v is None:
            environ.pop(k, None)
        else:
            environ[k] = v
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environ)


def run(s, *extra, **kw):
    proc = start(s, *extra, **kw)
    out, err = proc.communicate(timeout=120)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def calls(s):
    return [json.loads(line) for line in s.log.read_text().splitlines()] if s.log.exists() else []


def eval_calls(s):
    return [c for c in calls(s) if c["argv"][:2] == ["plugin", "eval"] and "--help" not in c["argv"]]


def ledger(s):
    return [json.loads(line) for line in s.ledger.read_text().splitlines()] if s.ledger.exists() else []


def key_forms():
    spec = importlib.util.spec_from_file_location("run_skill_eval_for_plugin_tests", str(HARNESS))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.key_forms(SECRET)


def test_plugin_eval_refuses_tool_grants_and_redacts_the_key(suite):
    # A forbidden grant in any case of the skill refuses the run, even in a case the glob does not select: the CLI's own
    # glob matching is not the wrapper's to predict.
    grants = {"prompt.md": ["Bash", "Bash(ls:*)", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "mcp__db__query"]}
    case = "skills/qa-demo/evals/plugin/trigger-en-02/"
    for tool in grants["prompt.md"]:
        rev = commit(suite, {case + "prompt.md": _case_files("qa-demo", "x", "p", "[Skill, Read, %s]" % tool)["x/prompt.md"]})
        r = run(suite, "--commit", rev, case="trigger-en-01")
        assert r.returncode == 3, (tool, r.stdout, r.stderr)
        assert tool.split("(")[0] in r.stderr and "trigger-en-02" in r.stderr
    rev = commit(suite, {case + "prompt.md": _case_files("qa-demo", "x", "p")["x/prompt.md"],      # clean again
                         case + "case.yaml": 'schema_version: "1.1"\nname: trigger-en-02\nexecution:\n  allowed_tools:\n'
                                             '    - Read\n    - Edit\n'})
    r = run(suite, "--commit", rev)
    assert r.returncode == 3 and "Edit" in r.stderr
    assert eval_calls(suite) == [] and not suite.ledger.exists()

    r = run(suite, "--commit", suite.base, MODE="leak")
    assert r.returncode == 0, r.stdout + r.stderr
    saved = [p for p in suite.out.rglob("*") if p.is_file()]
    assert {"result.json", "report.html", "trace.jsonl"} <= {p.name for p in saved}
    blobs = [p.read_bytes() for p in saved] + [r.stdout.encode(), r.stderr.encode(), suite.ledger.read_bytes()]
    for form in key_forms():
        assert not [b for b in blobs if form in b], form
    assert b"[REDACTED:ANTHROPIC_API_KEY]" in (suite.out / "result.json").read_bytes()
    assert b"[REDACTED:ANTHROPIC_API_KEY]" in (suite.out / "results" / "report.html").read_bytes()


@pytest.mark.parametrize("mode", ["nocost", "badcost", "noresult", "interrupted"])
def test_plugin_eval_unknown_cost_blocks_the_ledger(suite, mode):
    if mode == "interrupted":
        proc = start(suite, MODE="hang")
        deadline = time.time() + 60
        while not eval_calls(suite) and time.time() < deadline:
            time.sleep(0.1)
        assert eval_calls(suite), "the fake CLI never started"
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=60)
        returncode = proc.returncode
    else:
        returncode = run(suite, MODE=mode).returncode
    assert returncode == 1
    start_line, end = ledger(suite)
    assert start_line["event"] == "start" and end["event"] == "end" and end["run_id"] == start_line["run_id"]
    assert end["cost_usd"] is None and end["cost_unknown"] is True and end["status"] != "ok"
    if mode == "interrupted":
        assert end["status"] == "interrupted"
    r = run(suite)
    assert r.returncode == 3 and "unknown cost" in r.stderr
    assert len(eval_calls(suite)) == 1 and len(ledger(suite)) == 2


def test_plugin_eval_a_run_with_no_end_line_blocks_the_ledger(suite):
    suite.ledger.parent.mkdir()
    suite.ledger.write_text(json.dumps({"event": "start", "run_id": "inflight", "timestamp": "t"}) + "\n")
    r = run(suite)
    assert r.returncode == 3 and "inflight" in r.stderr and eval_calls(suite) == []


def test_plugin_eval_uses_an_export_not_the_working_tree(suite):
    head = commit(suite, {"skills/qa-demo/evals/plugin/trigger-en-01/prompt.md":
                          _case_files("qa-demo", "x", "Second commit prompt.")["x/prompt.md"]})
    write(suite.repo, {"skills/qa-demo/evals/plugin/trigger-en-01/prompt.md":
                       _case_files("qa-demo", "x", "Uncommitted prompt.")["x/prompt.md"],
                       "skills/qa-demo/untracked.md": "not committed\n"})
    assert run(suite).returncode == 0
    assert run(suite, "--commit", suite.base).returncode == 0
    now, before = eval_calls(suite)
    for call in (now, before):
        root = call["argv"][2]
        assert os.path.basename(root) == "qa-engineering-suite"
        assert not os.path.realpath(root).startswith(os.path.realpath(str(suite.repo)))
        assert not os.path.exists(root)                                   # the export is deleted afterwards
        files = call["files"]
        assert "skills/qa-demo/SKILL.md" in files and "skills/qa-demo/untracked.md" not in files
        assert not [f for f in files if f.startswith("skills/") and "evals" in f.split("/")]
        assert json.loads(call["manifest"]) == {"name": "qa-engineering-suite"}
        eval_dir = call["argv"][call["argv"].index("--eval-dir") + 1]
        assert eval_dir == "plugin-evals/qa-demo" and not eval_dir.startswith("skills/") and not os.path.isabs(eval_dir)
        staged = sorted(f for f in files if f.startswith(eval_dir + "/"))
        assert staged and not [f for f in staged if f.endswith("prompts.json") or "/results/" in f]
        assert not [f for f in files if f.startswith("plugin-evals/") and not f.startswith(eval_dir + "/")]
        assert sorted(call["prompts"]) == ["trigger-en-01", "trigger-en-02"]
    assert "Second commit prompt." in now["prompts"]["trigger-en-01"]
    assert "Committed prompt one." in before["prompts"]["trigger-en-01"]
    assert ledger(suite)[1]["commit"] == head and ledger(suite)[3]["commit"] == suite.base
    work = suite.repo / "skills/qa-demo/evals/plugin/trigger-en-01/prompt.md"
    assert "Uncommitted prompt." in work.read_text() and "runs: 3" in work.read_text()   # the working tree is untouched
    assert not (suite.repo / ".claude-plugin" / "plugin.json").exists()


def test_plugin_eval_never_uses_the_callers_config(suite):
    real_home, real_config = suite.root / "realhome", suite.root / "realhome" / ".claude"
    real_config.mkdir(parents=True)
    (real_config / "settings.json").write_text("{}")
    (real_home / ".claude.json").write_text("{}")
    snapshot = sorted((str(p), p.stat().st_mtime_ns) for p in real_home.rglob("*"))
    r = run(suite, env={"HOME": str(real_home), "CLAUDE_CONFIG_DIR": str(real_config), "SOME_OTHER_SECRET": "x",
                        "GIT_DIR": "/nowhere", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"})
    assert r.returncode == 0, r.stdout + r.stderr
    probes = [c for c in calls(suite) if "--help" in c["argv"]]
    assert probes and eval_calls(suite)
    for call in probes + eval_calls(suite):
        env = call["env"]
        want = CLEAN | ({"ANTHROPIC_API_KEY"} if call in eval_calls(suite) else set())
        assert set(env) - OS_ADDED == want, sorted(env)
        assert env["DISABLE_TELEMETRY"] == "1" and env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        for name in ("HOME", "CLAUDE_CONFIG_DIR"):
            assert not os.path.realpath(env[name]).startswith(os.path.realpath(str(real_home)))
            assert not os.path.exists(env[name])                          # temporary, and gone afterwards
    assert eval_calls(suite)[0]["env"]["ANTHROPIC_API_KEY"] == SECRET
    assert sorted((str(p), p.stat().st_mtime_ns) for p in real_home.rglob("*")) == snapshot


def test_plugin_eval_passes_no_publish_and_the_remaining_budget(suite):
    suite.ledger.parent.mkdir()
    suite.ledger.write_text(json.dumps({"event": "start", "run_id": "old", "timestamp": "t"}) + "\n"
                            + json.dumps({"event": "end", "run_id": "old", "cost_usd": 1.25, "cost_unknown": False}) + "\n")
    r = run(suite, budget="5", runs="2")
    assert r.returncode == 0, r.stdout + r.stderr
    argv = eval_calls(suite)[0]["argv"]
    out = str(suite.out.resolve())
    assert argv == ["plugin", "eval", argv[2], "--eval-dir", "plugin-evals/qa-demo", "--ablation", "none", "--runs", "2",
                    "--model", "test-model", "--judge-model", "claude-haiku-5-5", "--threshold", "0", "--no-publish",
                    "--trust-plugin", "--json", os.path.join(out, "result.json"), "--output-dir", os.path.join(out, "results"),
                    "--max-cost-usd", "3.7500", "--case", "trigger-*"]
    assert not [a for a in argv if a.startswith(FORBIDDEN_FLAGS)]
    start_line, end = ledger(suite)[2:]
    assert start_line == {"event": "start", "run_id": end["run_id"], "timestamp": start_line["timestamp"],
                          "case_id": "plugin-eval:qa-demo", "config": "plugin-eval", "model": "test-model"}
    assert end["engine"] == "claude plugin eval" and end["skill"] == "qa-demo" and end["case_id"] == "plugin-eval:qa-demo"
    assert end["cases"] == ["trigger-en-01", "trigger-en-02"] and end["runs"] == 2 and end["exit_code"] == 0
    assert end["partial"] is False and end["cost_usd"] == 0.42 and end["cost_unknown"] is False
    assert end["duration_ms"] == 12000 and end["status"] == "ok"

    assert run(suite, "--judge-model", "judge-x").returncode == 0
    argv = eval_calls(suite)[1]["argv"]
    assert argv[argv.index("--judge-model") + 1] == "judge-x" and argv[argv.index("--max-cost-usd") + 1] == "3.3300"

    r = run(suite, budget="1.67")
    assert r.returncode == 3 and "budget reached" in r.stderr and len(eval_calls(suite)) == 2
    for flag in ("--no-publish", "--max-cost-usd"):
        r = run(suite, HELP_OMIT=[flag])
        assert r.returncode == 3 and flag in r.stderr and len(eval_calls(suite)) == 2


def test_plugin_eval_refuses_a_result_whose_runs_per_case_differ(suite):
    assert run(suite, runs="1").returncode == 0
    for prompt in eval_calls(suite)[0]["prompts"].values():           # the scratch copy carries the run count asked for
        assert "\nruns: 1\n" in prompt and "runs: 3" not in prompt
    r = run(suite, runs="1", MODE="runs3")
    assert r.returncode == 1
    end = ledger(suite)[-1]
    assert end["status"] == "runs_mismatch" and end["cost_usd"] == 0.42 and end["cost_unknown"] is False
    assert end["runs"] == 1 and "runs_mismatch" in r.stdout


def test_plugin_eval_takes_verdicts_from_each_runs_graders(suite):
    r = run(suite, PASS={"trigger-en-01": False})
    assert r.returncode == 0, r.stdout + r.stderr
    result = json.loads((suite.out / "result.json").read_text())
    assert result["aggregates"]["casesPassed"] == 2                     # what the CLI says under --threshold 0
    shown = {c["name"]: c for c in json.loads(r.stdout)["cases"]}
    assert (shown["trigger-en-01"]["passes"], shown["trigger-en-01"]["fails"]) == (0, 1)
    assert (shown["trigger-en-02"]["passes"], shown["trigger-en-02"]["fails"]) == (1, 0)
    summary = {c["name"]: c for c in json.loads((suite.out / "summary.json").read_text())["cases"]}
    assert summary["trigger-en-01"]["runs"][0]["passed"] is False
    assert summary["trigger-en-01"]["runs"][0]["graders"][0]["explanation"].startswith("Skill called 0x")


def test_plugin_eval_a_turn_limit_stop_is_not_a_failed_run(suite):
    # A trigger case allows a few turns: the run that loads the skill and then stops at the case's own limit is graded as
    # usual. Any other run error fails the invocation.
    r = run(suite, RUN_ERROR="exit 1: Reached maximum number of turns (3)")
    assert r.returncode == 0, r.stdout + r.stderr
    end = ledger(suite)[-1]
    assert end["status"] == "ok" and end["max_turns_stops"] == 2 and end["run_errors"] == 0
    r = run(suite, RUN_ERROR="exit 1: API Error: 529 overloaded")
    end = ledger(suite)[-1]
    assert r.returncode == 1 and end["status"] == "failed" and end["run_errors"] == 2 and end["max_turns_stops"] == 0


def test_plugin_eval_refuses_bad_arguments_before_anything_runs(suite):
    assert run(suite, env={"ANTHROPIC_API_KEY": None}).returncode == 3
    assert run(suite, env={"ANTHROPIC_API_KEY": ""}).returncode == 3
    assert run(suite, skill="../qa-demo").returncode == 2
    assert run(suite, skill="qa-missing").returncode == 2
    assert run(suite, runs="0").returncode == 2
    assert run(suite, budget="0").returncode == 2
    assert run(suite, case="a/b").returncode == 2
    assert run(suite, case="no-such-case-*").returncode == 2
    assert run(suite, "--commit", "no-such-rev").returncode == 2
    used = suite.root / "used"
    (used / "results").mkdir(parents=True)
    assert run(suite, out=used).returncode == 2
    assert eval_calls(suite) == [] and not suite.ledger.exists()
