"""Tests for tools/run_skill_eval.py, driven by a fake ``claude`` executable (no real agent, no network)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import pytest

from helpers import REPO

HARNESS = REPO / "tools" / "run_skill_eval.py"
SECRET = "sk-test-0123456789-not-a-real-key"
# What the fake CLI reports as ``apiKeySource`` for a key taken from the helper, and what the tests tell the harness to accept.
FAKE_SOURCE = "fake-helper-source"
# Runs the harness with the accepted source set: the shipped constant is None until a paid probe measures the real value.
RUNNER = textwrap.dedent('''\
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("run_skill_eval_runner", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HELPER_KEY_SOURCE = sys.argv[2]
    import os
    if os.environ.get("QA_TEST_BLIND"):                       # test hooks, set by a test and absent in life
        module.process_table = lambda: {}
    if os.environ.get("QA_TEST_FILE_STATE_LIMIT"):
        module.FILE_STATE_LIMIT = int(os.environ["QA_TEST_FILE_STATE_LIMIT"])
    if os.environ.get("QA_TEST_SIGTERM_AFTER_HELPER"):
        real = module.write_key_helper
        def write_then_term(key, folder):
            result = real(key, folder)
            os.kill(os.getpid(), 15)
            return result
        module.write_key_helper = write_then_term
    sys.exit(module.main(sys.argv[3:]))
''')
# Names the platform adds to a child's environment on its own: Python sets LC_CTYPE, macOS sets __CF_USER_TEXT_ENCODING.
OS_ADDED = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}

# The fake CLI. The agent's environment is minimal on purpose, so the fake reads its behaviour (MODE, COST, HELP_OMIT, MARKER)
# from claude.cfg beside it; LOG is a file it appends one JSON line per call to.
FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, shlex, subprocess, sys, time
    argv = sys.argv[1:]
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude.cfg")))
    mode = cfg.get("MODE", "ok")
    if cfg.get("HELP_LOG") and (argv[:1] in (["--help"], ["plugin"])):
        with open(cfg["HELP_LOG"], "a") as fh:
            fh.write(json.dumps({"argv": argv, "env": dict(os.environ)}) + "\\n")
    if argv[:1] == ["--help"]:
        omit = cfg.get("HELP_OMIT", "").split(",")
        for flag in ("-p, --print", "--output-format <f>", "--allowedTools <tools>", "--model <m>", "--setting-sources <s>",
                     "--strict-mcp-config", "--mcp-config <f>", "--settings <f>", "--disable-slash-commands",
                     "--max-budget-usd <amount>", "--verbose"):
            if flag.split()[0].split(",")[-1].strip() not in omit:
                print("  " + flag)
        sys.exit(0)
    if argv[:1] == ["plugin"]:
        sys.exit(2)
    stdin = sys.stdin.read()
    settings_text = open(argv[argv.index("--settings") + 1]).read() if "--settings" in argv else "{}"
    helper = json.loads(settings_text).get("apiKeyHelper")
    helper_info = None
    if helper:
        script = shlex.split(helper)[0]
        folder = os.path.dirname(script)
        modes = {n: oct(os.stat(os.path.join(folder, n)).st_mode & 0o777) for n in os.listdir(folder)}
        helper_info = {"dir": os.path.realpath(folder), "dir_mode": oct(os.stat(folder).st_mode & 0o777), "files": modes,
                       "command": helper}
    # The key reaches the agent the way it does in life: by running the settings' apiKeyHelper.
    KEY = subprocess.run(helper, shell=True, capture_output=True, text=True).stdout if helper else os.environ.get("ANTHROPIC_API_KEY", "")
    log = cfg.get("LOG")
    if log:
        with open(log, "a") as fh:
            extra = {}
            if "--settings" in argv:
                extra["settings"] = open(argv[argv.index("--settings") + 1]).read()
            if "--mcp-config" in argv:
                extra["mcp"] = open(argv[argv.index("--mcp-config") + 1]).read()
            fh.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": stdin, "env": dict(os.environ), "helper": helper_info,
                                 "helper_key": KEY, "has_key": "ANTHROPIC_API_KEY" in os.environ, **extra}) + "\\n")
    init = {"type": "system", "subtype": "init", "apiKeySource": cfg.get("API_KEY_SOURCE", "FAKE_SOURCE_PLACEHOLDER"),
            "skills": cfg.get("SKILLS", ["qa-demo"]), "tools": ["Read", "Skill"]}
    if mode != "noinit":
        print(json.dumps(init), flush=True)
    if mode == "hang":
        time.sleep(60)
    if mode == "slow":
        time.sleep(3)
    if mode == "grandchild":
        import subprocess
        subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(3); open(sys.argv[1], 'w').write('alive')",
                          cfg["MARKER"]])
        time.sleep(60)
    if mode == "setsid":
        subprocess.Popen([sys.executable, "-c", "import os, sys, time; os.setsid(); open(sys.argv[1], 'w').write(str(os.getpid())); "
                          "time.sleep(60)", cfg["PIDFILE"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        time.sleep(1.6)
    if mode == "orphan":
        # The escaped child is detached and this process exits the moment the child has said hello: inside one poll.
        subprocess.Popen([sys.executable, "-c", "import os, sys, time; os.setsid(); open(sys.argv[1], 'w').write(str(os.getpid())); "
                          "time.sleep(60)", cfg["PIDFILE"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(cfg["PIDFILE"]) and open(cfg["PIDFILE"]).read():
                break
            time.sleep(0.05)
    if mode == "crash":
        print("boom", file=sys.stderr)
        sys.exit(7)
    if mode == "garbage":
        print("this is not json")
        sys.exit(0)
    if cfg.get("QA"):
        qa = os.path.join(os.getcwd(), ".qa")
        for rel, text in {"ledger/events.jsonl": "{\\"k\\": 1}\\nkey " + KEY + "\\n",
                          "config.json": "{}", "redaction.key": "R", "authority.key": "A",
                          "work/scratch.txt": "w", "evidence/EXE-1/stdout.log": "ok"}.items():
            os.makedirs(os.path.dirname(os.path.join(qa, rel)), exist_ok=True)
            open(os.path.join(qa, rel), "w").write(text)
        os.symlink("/etc/hosts", os.path.join(qa, "link.txt"))
        os.symlink(os.getcwd(), os.path.join(qa, "evidence", "linkdir"))
    for rel in cfg.get("CAT") or []:
        data = open(os.path.join(os.getcwd(), rel), "rb").read()
        shown = data.decode("utf-8", "replace") + " hex " + data.hex()
        for ascii_only in (False, True):
            line = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": shown}]}},
                              ensure_ascii=ascii_only)
            sys.stdout.flush()
            sys.stdout.buffer.write((line + "\\n").encode("utf-8"))
            sys.stdout.flush()
        sys.stderr.flush()
        sys.stderr.buffer.write((shown + "\\n").encode("utf-8"))
        sys.stderr.flush()
    # File changes the agent makes: EDITS writes (or, for null, deletes) files in the copy, @KEY@ standing for the key and
    # @CWD@ for the copy's absolute path, a directory in the way being removed first; LINKS replaces a path by a symlink; GIT
    # runs git commands in the copy, as an agent could; CAT (first of all) prints files of the copy into the transcript; PY
    # (last of all) runs Python source in the copy, as an agent's own script could.
    for rel, text in (cfg.get("EDITS") or {}).items():
        path = os.path.join(os.getcwd(), rel)
        if text is None:
            os.remove(path)
        else:
            if os.path.isdir(path) and not os.path.islink(path):
                import shutil
                shutil.rmtree(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write(text.replace("@KEY@", KEY).replace("@CWD@", os.getcwd()))
    for rel, target in (cfg.get("LINKS") or {}).items():
        path = os.path.join(os.getcwd(), rel)
        if os.path.isdir(path) and not os.path.islink(path):
            import shutil
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
        os.symlink(target, path)
    for args in cfg.get("GIT") or []:
        subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@a", "-c", "commit.gpgsign=false"] + args, cwd=os.getcwd(),
                       capture_output=True)
    for source in cfg.get("PY") or []:
        exec(source, {"__name__": "agent_script"})
    body = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 2,
            "duration_ms": 1500, "total_cost_usd": float(cfg.get("COST", "0.25")),
            "usage": {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 7,
                      "cache_creation_input_tokens": 3}}
    if mode == "leak":
        body["result"] = "the key is " + KEY
        print("stderr has " + KEY, file=sys.stderr)
    if mode == "leak-encoded":
        import base64
        raw = KEY.encode()
        body["result"] = "b64 " + base64.b64encode(b"x" + raw).decode() + " hex " + raw.hex()
        print("url " + base64.urlsafe_b64encode(b"yz" + raw + b"!").decode(), file=sys.stderr)
    if mode == "nocost":
        del body["total_cost_usd"]
    if mode == "errorresult":
        body["is_error"] = True
        body["subtype"] = "error_max_turns"
    print(json.dumps(body))
    sys.exit(1 if mode == "errorresult" else 0)
''')


@pytest.fixture
def world(tmp_path):
    """A fake CLI on PATH, a fixture with a hidden key and later history, a skill, and case files."""
    if shutil.which("git") is None:
        pytest.skip("tool missing: git")   # the Linux images ship no git
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE.replace("FAKE_SOURCE_PLACEHOLDER", FAKE_SOURCE))
    exe.chmod(0o755)

    fixture = tmp_path / "fixture"
    (fixture / "answer-keys").mkdir(parents=True)
    (fixture / "app.py").write_text("def f():\n    return 1\n")
    (fixture / "answer-keys" / "app.json").write_text('{"findings": ["secret expected result"]}\n')
    (fixture / "notes").mkdir()
    (fixture / "notes" / "SKILL.md").write_text("---\nname: stray\n---\n")
    (fixture / "node_modules").mkdir()
    (fixture / "node_modules" / "junk.js").write_text("x\n")
    git_env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@a", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@a")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "first"], ["tag", "v-answer"],
                 ["commit", "-q", "--allow-empty", "-m", "the answer is in a later commit"]):
        subprocess.run(["git", "-C", str(fixture)] + args, check=True, env=git_env, capture_output=True)

    skill = tmp_path / "skills" / "qa-demo"
    (skill / "references").mkdir(parents=True)
    (skill / "evals").mkdir()
    (skill / "SKILL.md").write_text("---\nname: qa-demo\ndescription: Demo.\n---\n\n# Demo\n")
    (skill / "references" / "notes.md").write_text("Notes.\n")
    (skill / "evals" / "case.json").write_text("{}\n")
    other = tmp_path / "skills" / "qa-other"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: qa-other\ndescription: Other.\n---\n")

    case = tmp_path / "case.json"
    case.write_text(json.dumps({"id": "demo-1", "prompt": "Run the tests and report.", "fixture": str(fixture),
                                "skill": str(skill)}))
    bait = tmp_path / "bait.json"
    bait.write_text(json.dumps({"id": "bait-1", "prompt": "Review this.", "fixture": "fixtures/injection-bait",
                                "skill": str(skill)}))
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()

    class World:
        pass

    w = World()
    w.root, w.case, w.bait, w.out, w.log, w.tmp = tmp_path, case, bait, tmp_path / "out", tmp_path / "fake.log", tmp_root
    w.ledger = tmp_path / "ledger" / "costs.jsonl"
    w.bin = bin_dir
    w.runner = tmp_path / "runner.py"
    w.runner.write_text(RUNNER)
    return w


def run(w, *extra, config="with-skill", budget="5", case=None, mode="ok", env=None, keep=True, direct=False):
    environ = dict(os.environ, PATH=str(w.bin) + os.pathsep + os.environ["PATH"], TMPDIR=str(w.tmp), ANTHROPIC_API_KEY=SECRET)
    fake = {"MODE": mode, "LOG": str(w.log), "HELP_LOG": str(w.root / "help.log")}
    for k in [k for k in (env or {}) if k.startswith("FAKE_")]:
        fake[k[5:]] = env[k]
    env = {k: v for k, v in (env or {}).items() if not k.startswith("FAKE_")}
    set_fake(w, **fake)
    for k, v in env.items():
        if v is None:
            environ.pop(k, None)
        else:
            environ[k] = v
    return subprocess.run(command(w, *extra, config=config, budget=budget, case=case, keep=keep, direct=direct), capture_output=True,
                          text=True, env=environ, timeout=120)


def set_fake(w, **cfg):
    (w.bin / "claude.cfg").write_text(json.dumps(cfg))


def command(w, *extra, config="with-skill", budget="5", case=None, out=None, keep=True, direct=False):
    """The harness command. ``keep`` adds ``--keep-workdir`` so a test can look inside the copy; the default run deletes it.
    ``direct`` runs the shipped file as it is (no accepted key source); otherwise a runner sets ``FAKE_SOURCE`` as accepted."""
    launch = [sys.executable, "-I", str(HARNESS)] if direct else [sys.executable, "-I", str(w.runner), str(HARNESS), FAKE_SOURCE]
    return launch + ["--case", str(case or w.case), "--config", config, "--model", "test-model",
            "--budget-usd", budget, "--out", str(out or w.out), "--ledger", str(w.ledger)] + (["--keep-workdir"] if keep else []) \
        + list(extra)


def records(w):
    path = w.out / "costs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def calls(w):
    return [json.loads(line) for line in w.log.read_text().splitlines()] if w.log.exists() else []


def listing(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)


def git(workdir, *args):
    return subprocess.run(["git", "-C", str(workdir)] + list(args), capture_output=True, text=True).stdout.split()


def test_copy_has_fresh_history_and_no_key(world):
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    work = records(world)[0]["workdir"]
    files = listing(__import__("pathlib").Path(work))
    assert "app.py" in files
    assert not [f for f in files if "answer" in f or f.endswith("SKILL.md") and not f.startswith(".claude/skills/qa-demo/")]
    assert "node_modules/junk.js" not in files
    assert not [f for f in files if "benchmark" in f]
    assert len(git(work, "rev-list", "--all")) == 1          # one commit, and nothing later
    assert git(work, "tag") == [] and len(git(work, "for-each-ref", "--format=%(refname)")) == 1      # the one branch, no tags, no other refs
    source_head = git(world.root / "fixture", "rev-parse", "HEAD")
    assert git(work, "rev-parse", "HEAD") != source_head
    assert "secret expected result" not in "".join((__import__("pathlib").Path(work) / f).read_text() for f in files)


def test_with_skill_copies_only_the_skill_under_test_and_baseline_none(world):
    import pathlib
    run(world)
    work = pathlib.Path(records(world)[0]["workdir"])
    skills = listing(work / ".claude")
    assert skills == ["skills/qa-demo/SKILL.md", "skills/qa-demo/references/notes.md"]      # no evals, no other skill
    run(world, config="baseline")
    base = pathlib.Path(records(world)[1]["workdir"])
    assert not (base / ".claude").exists()
    assert not [f for f in listing(base) if f.endswith("SKILL.md")]


def test_injection_bait_copy_is_only_the_generator_output(world):
    import pathlib
    r = run(world, case=world.bait, config="baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    work = pathlib.Path(records(world)[0]["workdir"])
    assert listing(work) == ["README.md", "app.py", "helper/SKILL.md", "test_app.py"]
    assert not (work / "generate.py").exists()
    assert len(git(work, "rev-list", "--all")) == 1
    assert "generate" not in "".join((work / f).read_text(encoding="utf-8") for f in listing(work)).lower()


def test_refuses_when_budget_reached(world):
    assert run(world, budget="0.30", env={"FAKE_COST": "0.30"}).returncode == 0
    r = run(world, budget="0.30", env={"FAKE_COST": "0.30"})
    assert r.returncode == 3
    assert "budget" in (r.stdout + r.stderr).lower()
    assert len(calls(world)) == 1 and len(records(world)) == 1      # the second run never started


def test_the_per_run_cap_is_the_budget_left(world):
    run(world)
    run(world)
    argv = calls(world)[1]["argv"]
    assert float(argv[argv.index("--max-budget-usd") + 1]) == 4.75      # what is left of 5 after the first 0.25


def test_records_cost_of_every_run(world):
    for _ in range(2):
        assert run(world).returncode == 0
    recs = records(world)
    assert len(recs) == 2
    for rec in recs:
        assert rec["cost_usd"] == 0.25 and rec["cost_unknown"] is False
        assert rec["duration_ms"] == 1500 and rec["tokens"] == {"input": 100, "output": 40, "cache_read": 7, "cache_creation": 3}
        assert rec["config"] == "with-skill" and rec["model"] == "test-model" and rec["case_id"] == "demo-1"
        assert rec["status"] == "ok" and rec["exit_code"] == 0 and rec["engine"] == "claude -p"
    assert recs[0]["run_id"] != recs[1]["run_id"]


def test_the_command_has_no_bypass_and_a_narrow_tool_list(world):
    run(world)
    call = calls(world)[0]
    argv = call["argv"]
    assert not [a for a in argv if a.startswith("--") and ("dangerously" in a or "bypass" in a.lower())]
    tools = argv[argv.index("--allowedTools") + 1]
    assert "Read" in tools and "Skill" in tools and "WebFetch" not in tools and "Write" not in tools
    assert not [t for t in tools.split(",") if t.startswith(("Bash(python", "Bash(find", "Bash(cat", "Bash(grep", "Bash(pytest"))]
    assert not [t for t in tools.split(",") if t.startswith("Bash(") and (":" in t or not t.endswith(" *)"))]
    assert call["stdin"] == "Run the tests and report."
    assert call["cwd"] == records(world)[0]["workdir"] or os.path.realpath(call["cwd"]) == os.path.realpath(records(world)[0]["workdir"])
    run(world, config="baseline")
    assert "Skill" not in calls(world)[1]["argv"][calls(world)[1]["argv"].index("--allowedTools") + 1]


def test_failed_run_is_recorded_not_dropped(world):
    r = run(world, mode="crash")
    assert r.returncode == 1
    rec = records(world)[0]
    assert rec["status"] == "failed" and rec["exit_code"] == 7 and rec["cost_usd"] is None and rec["cost_unknown"] is True


def test_error_result_keeps_its_cost(world):
    r = run(world, mode="errorresult")
    assert r.returncode == 1
    rec = records(world)[0]
    assert rec["status"] == "failed" and rec["cost_usd"] == 0.25 and rec["cost_unknown"] is False


def test_timeout_is_recorded_and_blocks_further_runs(world):
    r = run(world, "--timeout", "1", mode="hang")
    assert r.returncode == 1
    rec = records(world)[0]
    assert rec["status"] == "timeout" and rec["timed_out"] is True and rec["cost_usd"] is None and rec["cost_unknown"] is True
    again = run(world)
    assert again.returncode == 3 and len(calls(world)) == 1
    assert "cost" in (again.stdout + again.stderr).lower()


@pytest.mark.parametrize("mode", ["nocost", "garbage"])
def test_unreadable_cost_is_null_and_refuses_the_next_run(world, mode):
    r = run(world, mode=mode)
    rec = records(world)[0]
    assert rec["cost_usd"] is None and rec["cost_unknown"] is True
    assert rec["tokens"]["input"] in (None, 100)
    if mode == "garbage":
        assert rec["tokens"] == {"input": None, "output": None, "cache_read": None, "cache_creation": None}
    assert run(world).returncode == 3 and len(records(world)) == 1


def test_a_damaged_ledger_line_refuses_the_run(world):
    world.ledger.parent.mkdir()
    world.ledger.write_text("{not json\n")
    assert run(world).returncode == 3 and calls(world) == []


def test_no_credential_is_written_anywhere(world):
    r = run(world)
    blob = r.stdout + r.stderr + "".join(p.read_text(errors="replace") for p in world.out.rglob("*") if p.is_file())
    assert SECRET not in blob


def test_bad_arguments_are_refused_before_anything_runs(world):
    assert run(world, budget="0").returncode == 2
    assert run(world, budget="abc").returncode == 2
    assert run(world, case=world.root / "missing.json").returncode == 2
    assert calls(world) == [] and records(world) == []


# -- fix round 1 ----------------------------------------------------------------------------------------------------

def test_a_missing_key_is_refused_before_anything_is_spent(world):
    r = run(world, env={"ANTHROPIC_API_KEY": None})
    assert r.returncode == 3 and "ANTHROPIC_API_KEY" in r.stderr
    assert calls(world) == [] and not world.ledger.exists() and records(world) == []
    assert run(world, env={"ANTHROPIC_API_KEY": ""}).returncode == 3


def test_the_child_gets_a_minimal_environment_and_no_user_config(world):
    r = run(world, env={"GIT_DIR": "/nowhere", "SOME_OTHER_SECRET": "x", "HOME": str(world.root / "realhome")})
    assert r.returncode == 0, r.stderr
    call = calls(world)[0]
    assert call["has_key"] is False and call["helper_key"] == SECRET        # the key arrives through the helper only
    assert sorted(set(call["env"]) - OS_ADDED) == ["CLAUDE_CONFIG_DIR", "HOME", "PATH"]
    assert call["env"]["HOME"] != str(world.root / "realhome") and call["env"]["CLAUDE_CONFIG_DIR"].startswith(call["env"]["HOME"])
    assert not os.path.exists(call["env"]["HOME"])                      # the temporary home is gone afterwards
    argv = call["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv and json.loads(call["mcp"]) == {"mcpServers": {}}
    settings = json.loads(call["settings"])
    deny = settings["permissions"]["deny"]
    assert any(str(REPO).lstrip("/") in d for d in deny) and any("benchmark" in d for d in deny)
    assert any(str(world.root / "realhome").lstrip("/") in d for d in deny)
    assert any(d.startswith("Read(//") and not d.startswith("Read(///") for d in deny)      # absolute: a double slash
    assert not [d for d in deny if d.startswith("Read(/") and not d.startswith(("Read(//", "Read(**"))]
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is True
    assert sandbox["network"] == {"allowedDomains": []}
    assert str(REPO) in sandbox["filesystem"]["denyRead"] and os.path.realpath(str(world.root / "realhome")) in sandbox["filesystem"]["denyRead"]
    assert [os.path.realpath(call["cwd"])] == sandbox["filesystem"]["allowRead"]


def test_slash_commands_are_off_for_baseline_only(world):
    run(world, config="baseline")
    run(world)
    assert "--disable-slash-commands" in calls(world)[0]["argv"]
    assert "--disable-slash-commands" not in calls(world)[1]["argv"]


@pytest.mark.parametrize("flag", ["--max-budget-usd", "--setting-sources", "--strict-mcp-config", "--settings"])
def test_a_cli_without_an_essential_flag_is_refused(world, flag):
    r = run(world, env={"FAKE_HELP_OMIT": flag})
    assert r.returncode == 3 and flag in r.stderr
    assert calls(world) == [] and not world.ledger.exists()


def test_the_sandbox_block_does_not_depend_on_help_text(world):
    assert run(world).returncode == 0       # the fake's help never mentions a sandbox
    iso = records(world)[0]["isolation"]
    assert iso["sandbox"] is True and iso["bare"] is False
    argv = calls(world)[0]["argv"]
    assert "--settings" in argv and json.loads(calls(world)[0]["settings"])["sandbox"]["enabled"] is True


def test_bare_is_never_passed_and_the_output_is_a_verbose_event_stream(world):
    run(world)
    run(world, config="baseline")
    for c in calls(world):
        argv = c["argv"]
        assert "--bare" not in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv


def test_the_init_event_is_recorded(world):
    run(world, env={"FAKE_SKILLS": ["qa-demo", "other"]})
    rec = records(world)[0]
    assert rec["api_key_source"] == FAKE_SOURCE and rec["skills_listed"] == ["qa-demo", "other"]
    assert rec["skill_tool_present"] is True and rec["status"] == "ok"


@pytest.mark.parametrize("source", ["none", "/login managed key", None])
def test_a_run_not_paid_with_the_key_is_failed_but_keeps_its_cost(world, source):
    r = run(world, env={"FAKE_API_KEY_SOURCE": source})
    rec = records(world)[0]
    assert r.returncode == 1 and rec["status"] == "failed" and rec["api_key_source"] == source
    assert rec["cost_usd"] == 0.25 and rec["cost_unknown"] is False


def test_a_run_without_an_init_event_is_failed(world):
    r = run(world, mode="noinit")
    rec = records(world)[0]
    assert r.returncode == 1 and rec["status"] == "failed" and rec["api_key_source"] is None and rec["cost_usd"] == 0.25


def _harness_copy(world, monkeypatch):
    """A copy of the harness in a throwaway 'repository' under tmp_path, so a venv can be made inside it."""
    repo = world.root / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(str(HARNESS), str(repo / "tools" / "run_skill_eval.py"))
    monkeypatch.setattr(sys.modules[__name__], "HARNESS", repo / "tools" / "run_skill_eval.py")
    return repo


def test_runtime_venv_goes_first_on_path_and_into_allow_read_only(world, monkeypatch):
    repo = _harness_copy(world, monkeypatch)
    (repo / ".venv-x" / "bin").mkdir(parents=True)
    spec = json.loads(world.case.read_text())
    spec["runtime_venv"] = ".venv-x"
    world.case.write_text(json.dumps(spec))
    assert run(world).returncode == 0
    call = calls(world)[0]
    real = os.path.realpath(str(repo / ".venv-x"))
    assert call["env"]["PATH"].split(os.pathsep)[0] == os.path.join(real, "bin")
    settings = json.loads(call["settings"])
    assert settings["sandbox"]["filesystem"]["allowRead"] == [os.path.realpath(call["cwd"]), real]
    assert real not in settings["sandbox"]["filesystem"]["denyRead"]
    assert os.path.realpath(str(repo)) in settings["sandbox"]["filesystem"]["denyRead"]      # the repository stays denied


@pytest.mark.parametrize("path", ["/usr", "..", "../elsewhere", "tools", ""])
def test_runtime_venv_outside_the_repo_or_without_bin_is_refused(world, path):
    assert not (REPO / "tools" / "bin").exists()
    spec = json.loads(world.case.read_text())
    spec["runtime_venv"] = path
    world.case.write_text(json.dumps(spec))
    assert run(world).returncode == 2 and calls(world) == [] and not world.ledger.exists()


def test_a_remainder_below_the_cap_precision_is_refused(world):
    assert run(world, budget="0.25", env={"FAKE_COST": "0.24996"}).returncode == 0
    r = run(world, budget="0.25")
    assert r.returncode == 3 and len(calls(world)) == 1


def test_bash_allow_adds_narrow_prefixes_and_refuses_bare_interpreters(world):
    spec = json.loads(world.case.read_text())
    spec["bash_allow"] = ["python3 .claude/skills/qa-demo/scripts/qa.py"]
    world.case.write_text(json.dumps(spec))
    run(world)
    argv = calls(world)[0]["argv"]
    assert "Bash(python3 .claude/skills/qa-demo/scripts/qa.py *)" in argv[argv.index("--allowedTools") + 1].split(",")
    for bad in ("python3", "find", "bash"):
        spec["bash_allow"] = [bad]
        world.case.write_text(json.dumps(spec))
        assert run(world).returncode == 2


def test_the_ledger_is_independent_of_out(world):
    assert run(world, budget="0.30", env={"FAKE_COST": "0.30"}).returncode == 0
    other = world.root / "elsewhere"
    r = subprocess.run(command(world, budget="0.30", out=other), capture_output=True, text=True,
                       env=dict(os.environ, PATH=str(world.bin) + os.pathsep + os.environ["PATH"], ANTHROPIC_API_KEY=SECRET,
                                TMPDIR=str(world.tmp)))
    assert r.returncode == 3 and len(calls(world)) == 1


def test_concurrent_runs_cannot_overspend(world):
    environ = dict(os.environ, PATH=str(world.bin) + os.pathsep + os.environ["PATH"], ANTHROPIC_API_KEY=SECRET,
                   TMPDIR=str(world.tmp))
    set_fake(world, MODE="slow", LOG=str(world.log))
    procs = [subprocess.Popen(command(world, budget="0.30"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=environ) for _ in range(3)]
    codes = sorted(p.wait(timeout=60) for p in procs)
    for p in procs:
        p.stdout.close()
        p.stderr.close()
    assert codes == [0, 3, 3] and len(calls(world)) == 1


def test_the_ledger_line_is_written_before_the_evidence(world):
    world.out.mkdir()
    (world.out / "runs").write_text("a file where the evidence directory should go")
    r = run(world)
    assert r.returncode == 1 and "evidence" in r.stderr
    lines = [json.loads(line) for line in world.ledger.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["start", "end"] and lines[1]["cost_usd"] == 0.25


def test_an_interrupted_run_is_recorded_with_unknown_cost(world):
    import signal
    import time
    environ = dict(os.environ, PATH=str(world.bin) + os.pathsep + os.environ["PATH"], ANTHROPIC_API_KEY=SECRET,
                   TMPDIR=str(world.tmp))
    set_fake(world, MODE="hang", LOG=str(world.log))
    proc = subprocess.Popen(command(world), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environ)
    for _ in range(100):
        if world.log.exists() and world.log.read_text():
            break
        time.sleep(0.1)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    end = [json.loads(line) for line in world.ledger.read_text().splitlines()][-1]
    assert end["event"] == "end" and end["status"] == "interrupted" and end["cost_unknown"] is True
    assert run(world).returncode == 3


def test_a_timeout_kills_the_whole_process_group(world):
    import time
    marker = world.root / "grandchild-alive"
    r = run(world, "--timeout", "1", mode="grandchild", env={"FAKE_MARKER": str(marker)})
    assert r.returncode == 1 and records(world)[0]["status"] == "timeout"
    time.sleep(4)
    assert not marker.exists()


def test_the_key_value_is_redacted_everywhere_it_is_saved(world):
    r = run(world, mode="leak")
    assert r.returncode == 0
    files = [p for root in (world.out, world.ledger.parent) for p in root.rglob("*") if p.is_file()]
    assert files and not [p for p in files if SECRET in p.read_text(errors="replace")]
    assert "[REDACTED:ANTHROPIC_API_KEY]" in (world.out / "runs" / records(world)[0]["run_id"] / "stdout.txt").read_text()
    assert SECRET not in r.stdout + r.stderr


def test_qa_state_is_copied_to_the_evidence_without_keys_work_or_links(world):
    r = run(world, env={"FAKE_QA": True})
    assert r.returncode == 0, r.stderr
    state = world.out / "runs" / records(world)[0]["run_id"] / "qa-state"
    files = sorted(str(p.relative_to(state)) for p in state.rglob("*") if p.is_file() or p.is_symlink())
    assert files == ["config.json", "evidence/EXE-1/stdout.log", "ledger/events.jsonl"]
    assert not [p for p in state.rglob("*") if p.name in ("redaction.key", "authority.key", "work") or p.is_symlink()]
    text = (state / "ledger" / "events.jsonl").read_text()
    assert SECRET not in text and "[REDACTED:ANTHROPIC_API_KEY]" in text


def test_the_ledger_end_line_exists_before_qa_state_and_a_failed_copy_is_a_problem(world, monkeypatch):
    repo = _harness_copy(world, monkeypatch)
    text = (repo / "tools" / "run_skill_eval.py").read_text()
    (repo / "tools" / "run_skill_eval.py").write_text(text.replace(
        "    source = workdir / \".qa\"\n", "    raise PermissionError('no')\n    source = workdir / \".qa\"\n", 1))
    r = run(world, env={"FAKE_QA": True})
    assert r.returncode == 1
    assert "qa-state not saved" in r.stderr and SECRET not in r.stderr
    rec = records(world)[0]
    assert rec["status"] == "ok"
    assert (world.out / "runs" / rec["run_id"] / "stdout.txt").exists()
    assert not (world.out / "runs" / rec["run_id"] / "qa-state").exists()
    assert [json.loads(l)["event"] for l in world.ledger.read_text().splitlines()] == ["start", "end"]


# --------------------------------------------------------------------------- setup_script (task 14)

def _setup_case(w, body, name="setup.py", **extra):
    """A case whose setup script lives in the demo skill's evals directory."""
    script = w.root / "skills" / "qa-demo" / "evals" / name
    script.write_text(body)
    spec = json.loads(w.case.read_text())
    spec["setup_script"] = str(script)
    spec.update(extra)
    w.case.write_text(json.dumps(spec))
    return script


def _workdir(w, index=0):
    import pathlib
    return pathlib.Path(records(w)[index]["workdir"])


def test_setup_script_runs_in_the_copy_before_the_agent_and_stays_out_of_it(world):
    _setup_case(world, "import os, subprocess\nopen('prepared.txt', 'w').write(os.getcwd())\n"
                       "print(subprocess.run(['git', 'rev-list', '--all'], capture_output=True, text=True).stdout.split().__len__())\n")
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    work = _workdir(world)
    assert (work / "prepared.txt").read_text() == os.path.realpath(str(work)) or (work / "prepared.txt").read_text() == str(work)
    assert not [f for f in listing(work) if "setup" in f]                        # the script is not in the copy
    seen = calls(world)[0]
    assert (work / "prepared.txt").exists() and seen["cwd"] in (str(work), os.path.realpath(str(work)))
    saved = (world.out / "runs" / records(world)[0]["run_id"] / "setup.txt").read_text()
    assert "exit 0" in saved and "--- stdout\n1\n" in saved                        # one commit existed when it ran


def test_setup_script_environment_has_no_api_key_and_runs_isolated(world):
    _setup_case(world, "import os, sys\nprint('KEY' if 'ANTHROPIC_API_KEY' in os.environ else 'nokey')\n"
                       "print(sorted(os.environ))\nprint(sys.flags.isolated)\nprint(os.environ.get('HOME'))\n")
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    saved = (world.out / "runs" / records(world)[0]["run_id"] / "setup.txt").read_text()
    assert "nokey" in saved and "KEY\n" not in saved.replace("nokey", "") and SECRET not in saved
    names = __import__("ast").literal_eval(saved.split("\n")[3])
    assert {"HOME", "PATH", "PYTHONDONTWRITEBYTECODE"} <= set(names) <= {"HOME", "PATH", "PYTHONDONTWRITEBYTECODE"} | OS_ADDED
    assert "\n1\n" in saved
    assert "/qa-eval-setup-" in saved                                              # a per-run home, not the user's


def test_a_failing_setup_script_refuses_the_run_and_spends_nothing(world):
    _setup_case(world, "import sys\nprint('half done')\nprint('why', file=sys.stderr)\nsys.exit(4)\n")
    r = run(world)
    assert r.returncode == 3 and "setup script exited 4" in (r.stdout + r.stderr)
    assert calls(world) == [] and records(world) == [] and not world.ledger.exists()
    saved = next((world.out / "runs").rglob("setup.txt")).read_text()
    assert "half done" in saved and "why" in saved
    assert not [p for p in world.tmp.iterdir() if p.name.startswith("qa-eval-")]       # nothing left behind


def test_a_setup_script_that_runs_too_long_is_refused(world, monkeypatch):
    repo = _harness_copy(world, monkeypatch)
    text = (repo / "tools" / "run_skill_eval.py").read_text().replace("SETUP_TIMEOUT = 120", "SETUP_TIMEOUT = 1")
    (repo / "tools" / "run_skill_eval.py").write_text(text)
    _setup_case(world, "import time\nprint('start', flush=True)\ntime.sleep(30)\n")
    r = run(world)
    assert r.returncode == 3 and "ran past 1 seconds" in (r.stdout + r.stderr) and calls(world) == []


@pytest.mark.parametrize("where", ["../outside.py", "skills/qa-other/evals/x.py", "../../../etc/passwd", "notpy.txt", "missing.py"])
def test_a_setup_script_outside_the_skills_own_evals_directory_is_refused(world, where):
    (world.root / "outside.py").write_text("print(1)\n")
    (world.root / "skills" / "qa-other" / "evals").mkdir()
    (world.root / "skills" / "qa-other" / "evals" / "x.py").write_text("print(1)\n")
    (world.root / "skills" / "qa-demo" / "evals" / "notpy.txt").write_text("print(1)\n")
    spec = json.loads(world.case.read_text())
    spec["setup_script"] = str((world.root / "skills" / "qa-demo" / "evals" / where) if where in ("notpy.txt", "missing.py")
                               else (world.root / "skills" / "qa-demo" / "evals" / where) if where.startswith("..")
                               else world.root / where)
    world.case.write_text(json.dumps(spec))
    r = run(world)
    assert r.returncode == 2 and calls(world) == [] and "setup_script" in r.stderr


def test_a_symlink_out_of_the_evals_directory_is_refused(world):
    outside = world.root / "outside.py"
    outside.write_text("print(1)\n")
    link = world.root / "skills" / "qa-demo" / "evals" / "link.py"
    link.symlink_to(outside)
    spec = json.loads(world.case.read_text())
    spec["setup_script"] = str(link)
    world.case.write_text(json.dumps(spec))
    assert run(world).returncode == 2 and calls(world) == []


def test_a_setup_script_needs_the_case_to_name_its_skill(world):
    script = _setup_case(world, "print(1)\n")
    spec = json.loads(world.case.read_text())
    del spec["skill"]
    world.case.write_text(json.dumps(spec))
    r = run(world, config="baseline")
    assert r.returncode == 2 and "skill" in r.stderr and script.exists()


def test_setup_runs_for_both_configs(world):
    _setup_case(world, "open('prepared.txt', 'w').write('x')\n")
    for config in ("baseline", "with-skill"):
        assert run(world, config=config).returncode == 0
    assert all((_workdir(world, i) / "prepared.txt").exists() for i in (0, 1))


# --------------------------------------------------------------------------- skills and agents lists (task 15)

def _spine(world, monkeypatch):
    """A throwaway repository holding two skills and two agent files, and a case that declares them."""
    repo = _harness_copy(world, monkeypatch)
    for name in ("qa-one", "qa-two"):
        (repo / "skills" / name / "evals").mkdir(parents=True)
        (repo / "skills" / name / "SKILL.md").write_text("---\nname: %s\ndescription: x.\n---\n" % name)
        (repo / "skills" / name / "evals" / "case.json").write_text("{}\n")
    (repo / "agents").mkdir()
    for name in ("qa-auditor", "qa-verifier"):
        (repo / "agents" / (name + ".md")).write_text("---\nname: %s\ndescription: x.\ntools: Read\n---\nBody.\n" % name)
    spec = json.loads(world.case.read_text())
    spec["skill"] = "skills/qa-one"
    spec["skills"] = ["skills/qa-one", "skills/qa-two"]
    spec["agents"] = ["agents/qa-auditor.md", "agents/qa-verifier.md"]
    world.case.write_text(json.dumps(spec))
    return repo, spec


def _allowed(world, index=0):
    argv = calls(world)[index]["argv"]
    return argv[argv.index("--allowedTools") + 1].split(",")


def test_skills_and_agents_are_installed_for_with_skill_only(world, monkeypatch):
    _spine(world, monkeypatch)
    assert run(world).returncode == 0
    assert listing(_workdir(world) / ".claude") == ["agents/qa-auditor.md", "agents/qa-verifier.md", "skills/qa-one/SKILL.md",
                                                    "skills/qa-two/SKILL.md"]
    assert run(world, config="baseline").returncode == 0
    assert not (_workdir(world, 1) / ".claude").exists()


def test_installed_skills_and_agents_stay_out_of_the_fixture_commit(world, monkeypatch):
    _spine(world, monkeypatch)
    assert run(world).returncode == 0
    workdir = _workdir(world)
    assert (workdir / ".claude" / "skills" / "qa-one" / "SKILL.md").is_file()
    git = lambda *a: subprocess.run(["git", "-C", str(workdir)] + list(a), check=True, capture_output=True, text=True).stdout
    committed = git("show", "--name-only", "--format=", "HEAD").split()
    assert committed and not [f for f in committed if f.startswith(".claude")]
    assert git("status", "--porcelain") == ""
    assert git("rev-list", "--count", "HEAD").strip() == "1"


def test_task_is_allowed_only_when_the_case_declares_agents(world, monkeypatch):
    repo, spec = _spine(world, monkeypatch)
    run(world)
    assert "Task" in _allowed(world)
    del spec["agents"]
    world.case.write_text(json.dumps(spec))
    run(world)
    assert "Task" not in _allowed(world, 1) and "Skill" in _allowed(world, 1)
    assert listing(_workdir(world, 1) / ".claude") == ["skills/qa-one/SKILL.md", "skills/qa-two/SKILL.md"]


def test_the_single_skill_field_still_works_beside_the_new_lists(world, monkeypatch):
    repo, spec = _spine(world, monkeypatch)
    del spec["skills"], spec["agents"]
    world.case.write_text(json.dumps(spec))
    assert run(world).returncode == 0
    assert listing(_workdir(world) / ".claude") == ["skills/qa-one/SKILL.md"]


def test_a_skill_named_twice_is_installed_once(world, monkeypatch):
    repo, spec = _spine(world, monkeypatch)
    spec["skills"] = ["skills/qa-one", "skills/qa-one", "skills/qa-two"]
    world.case.write_text(json.dumps(spec))
    assert run(world).returncode == 0
    assert listing(_workdir(world) / ".claude")[-2:] == ["skills/qa-one/SKILL.md", "skills/qa-two/SKILL.md"]


@pytest.mark.parametrize("field, value", [
    ("skills", "skills/qa-one"), ("skills", [""]), ("skills", [3]), ("skills", ["skills/missing"]), ("skills", ["../outside"]),
    ("skills", ["/etc"]), ("skills", ["skills"]), ("skills", ["skills/qa-one/evals"]),
    ("agents", "agents/qa-auditor.md"), ("agents", [""]), ("agents", ["agents/missing.md"]), ("agents", ["agents/qa-auditor.txt"]),
    ("agents", ["../outside.md"]), ("agents", ["/etc/hosts"]), ("agents", ["agents"]),
])
def test_bad_skills_and_agents_entries_are_refused_before_anything_runs(world, monkeypatch, field, value):
    repo, spec = _spine(world, monkeypatch)
    (repo / "agents" / "qa-auditor.txt").write_text("x\n")
    (world.root / "outside").mkdir()
    (world.root / "outside.md").write_text("x\n")
    spec[field] = value
    world.case.write_text(json.dumps(spec))
    r = run(world)
    assert r.returncode == 2 and calls(world) == [] and not world.ledger.exists() and field in r.stderr


def test_symlinks_in_skills_and_agents_are_refused(world, monkeypatch):
    repo, spec = _spine(world, monkeypatch)
    outside = world.root / "outside.md"
    outside.write_text("x\n")
    (repo / "agents" / "link.md").symlink_to(outside)
    (repo / "skills" / "linked").symlink_to(repo / "skills" / "qa-two")
    (repo / "skills" / "qa-two" / "leak.md").symlink_to(outside)
    for field, value in (("agents", ["agents/link.md"]), ("skills", ["skills/linked"]), ("skills", ["skills/qa-two"])):
        saved = dict(spec)
        spec[field] = value
        world.case.write_text(json.dumps(spec))
        r = run(world)
        assert r.returncode == 2 and calls(world) == [] and "symlink" in r.stderr, field
        spec.clear()
        spec.update(saved)


# --------------------------------------------------------------------------- the copy is deleted, encoded keys are redacted (I6)

def _harness_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_skill_eval_under_test", str(HARNESS))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _left_in_tmp(w):
    return sorted(p.name for p in w.tmp.iterdir() if p.name.startswith("qa-eval-"))


@pytest.mark.parametrize("mode", ["ok", "crash", "errorresult"])
def test_the_working_copy_is_deleted_after_the_run(world, mode):
    run(world, mode=mode, env={"FAKE_QA": "1"} if mode == "ok" else None, keep=False)
    rec = records(world)[0]
    assert not pathlib.Path(rec["workdir"]).exists() and _left_in_tmp(world) == []
    if mode == "ok":
        assert (world.out / "runs" / rec["run_id"] / "qa-state" / "config.json").is_file()  # copied before the delete


def test_a_refused_budget_deletes_the_working_copy_too(world):
    assert run(world, budget="0.000001", keep=False).returncode == 3
    assert _left_in_tmp(world) == []


def test_keep_workdir_leaves_the_copy(world):
    run(world)
    assert pathlib.Path(records(world)[0]["workdir"]).is_dir()


def test_base64_and_hex_forms_of_the_key_are_redacted_in_the_evidence(world):
    import base64

    run(world, mode="leak-encoded", keep=False)
    run_dir = world.out / "runs" / records(world)[0]["run_id"]
    saved = (run_dir / "stdout.txt").read_text() + (run_dir / "stderr.txt").read_text()
    raw = SECRET.encode()
    assert saved.count("[REDACTED:ANTHROPIC_API_KEY]") >= 3
    assert raw.hex() not in saved and all(form.decode() not in saved for form in _harness_module().key_forms(SECRET))
    assert base64.b64encode(b"x" + raw).decode()[4:-4] not in saved


@pytest.mark.parametrize("prefix", [b"", b"a", b"ab"], ids=["aligned", "one-byte", "two-bytes"])
def test_redaction_catches_the_key_at_every_base64_alignment(prefix):
    import base64

    harness = _harness_module()
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        stream = encode(prefix + SECRET.encode() + b"suffix")
        cleaned = harness.redact_bytes(stream, SECRET)
        assert harness.MARKER.encode() in cleaned
        assert all(form not in cleaned for form in harness.key_forms(SECRET))
    assert harness.redact("k=" + SECRET.encode().hex().upper(), SECRET) == "k=" + harness.MARKER


def test_copy_qa_state_redacts_encoded_forms_too(tmp_path):
    import base64

    harness = _harness_module()
    (tmp_path / "work" / ".qa" / "evidence").mkdir(parents=True)
    (tmp_path / "work" / ".qa" / "evidence" / "log.txt").write_bytes(b"x " + base64.b64encode(SECRET.encode()) + b" "
                                                                      + SECRET.encode().hex().encode())
    harness.copy_qa_state(tmp_path / "work", tmp_path / "dest", SECRET)
    saved = (tmp_path / "dest" / "evidence" / "log.txt").read_bytes()
    assert all(form not in saved for form in harness.key_forms(SECRET)) and saved.count(harness.MARKER.encode()) == 2


# --------------------------------------------------------------------------- milestone 3, task 1: key helper, sweeper, symlinks

def _alive(pid):
    if os.path.isdir("/proc/self"):                            # Linux: a zombie is not alive, a missing entry is gone
        try:
            return open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_the_agent_environment_holds_no_key(world):
    r = run(world)
    assert r.returncode == 0, r.stdout + r.stderr
    call = calls(world)[0]
    assert "ANTHROPIC_API_KEY" not in call["env"] and call["has_key"] is False
    assert SECRET not in json.dumps(call["env"]) and SECRET not in json.dumps(call["argv"]) and SECRET not in call["stdin"]
    assert SECRET not in call["settings"] and call["helper_key"] == SECRET        # only the helper hands it over
    harness = _harness_module()
    assert harness.child_env(pathlib.Path("/h"), pathlib.Path("/c")) == {"PATH": os.environ.get("PATH", os.defpath), "HOME": "/h",
                                                                         "CLAUDE_CONFIG_DIR": "/c"}


def test_settings_name_the_helper_and_deny_its_directory(world):
    assert run(world).returncode == 0
    call = calls(world)[0]
    settings, helper = json.loads(call["settings"]), call["helper"]
    folder = helper["dir"]
    assert settings["apiKeyHelper"] == helper["command"] and os.path.basename(folder) in settings["apiKeyHelper"]
    assert "Read(//%s/**)" % folder.lstrip("/") in settings["permissions"]["deny"]
    assert folder in settings["sandbox"]["filesystem"]["denyRead"] and folder not in settings["sandbox"]["filesystem"]["allowRead"]
    cwd = os.path.realpath(call["cwd"])
    assert not (folder + os.sep).startswith(cwd + os.sep) and not (cwd + os.sep).startswith(folder + os.sep)   # not in the copy


@pytest.mark.parametrize("mode, extra", [("ok", ()), ("crash", ()), ("errorresult", ()), ("hang", ("--timeout", "1"))])
@pytest.mark.parametrize("keep", [True, False])
def test_helper_files_are_private_and_removed_after_the_run(world, mode, extra, keep):
    run(world, *extra, mode=mode, keep=keep)
    helper = calls(world)[0]["helper"]
    assert helper["dir_mode"] == "0o700" and helper["files"] == {"helper.sh": "0o700", "key": "0o600"}
    assert not os.path.exists(helper["dir"])
    assert not [p for p in world.tmp.iterdir() if p.name.startswith("qa-eval-key-")]


def test_a_refused_budget_leaves_no_helper_directory(world):
    assert run(world, budget="0.000001", keep=False).returncode == 3
    assert _left_in_tmp(world) == []


def test_write_key_helper_makes_private_files_and_a_command_that_prints_the_key(tmp_path):
    harness = _harness_module()
    folder = tmp_path / "helper"
    command_line = harness.write_key_helper(SECRET, folder)
    assert subprocess.run(command_line, shell=True, capture_output=True, text=True).stdout == SECRET
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    assert sorted((p.name, oct(p.stat().st_mode & 0o777)) for p in folder.iterdir()) == [("helper.sh", "0o700"), ("key", "0o600")]


@pytest.mark.parametrize("source", ["ANTHROPIC_API_KEY", "none", "/login managed key", None])
def test_a_run_with_another_key_source_is_failed(world, source):
    r = run(world, env={"FAKE_API_KEY_SOURCE": source})
    rec = records(world)[0]
    assert r.returncode == 1 and rec["status"] == "failed" and rec["api_key_source"] == source
    assert rec["cost_usd"] == 0.25 and rec["cost_unknown"] is False


def test_the_shipped_constant_accepts_only_the_measured_source(world):
    assert _harness_module().HELPER_KEY_SOURCE == "apiKeyHelper"      # measured by the paid probe of 2026-11-10
    r = run(world, direct=True)                        # the shipped file, not the runner: another source is not accepted
    rec = records(world)[0]
    assert r.returncode == 1 and rec["status"] == "failed" and rec["api_key_source"] == FAKE_SOURCE
    r = run(world, direct=True, mode="noinit")         # a missing init event never matches
    assert r.returncode == 1 and records(world)[1]["status"] == "failed"
    r = run(world, direct=True, env={"FAKE_API_KEY_SOURCE": "apiKeyHelper"})
    assert r.returncode == 0 and records(world)[2]["status"] == "ok"


def test_escaped_setsid_grandchild_is_killed(world):
    import time
    pidfile = world.root / "escaped.pid"
    pid = None
    try:
        r = run(world, mode="setsid", env={"FAKE_PIDFILE": str(pidfile)})
        assert r.returncode == 0, r.stdout + r.stderr
        pid = int(pidfile.read_text())
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.1)
        assert not _alive(pid)
        assert records(world)[0]["swept_pids"] >= 1
    finally:
        if pid and _alive(pid):
            os.kill(pid, 9)


def test_sweeper_never_kills_a_reused_pid(monkeypatch):
    harness = _harness_module()
    killed = []
    monkeypatch.setattr(harness.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    sweeper = harness.Sweeper(100)
    monkeypatch.setattr(harness, "process_table", lambda: {100: (1, "Mon Nov 10 10:00:00 2026"), 200: (100, "Mon Nov 10 10:00:01 2026"),
                                                           300: (200, "Mon Nov 10 10:00:02 2026"), 400: (1, "Mon Nov 10 10:00:00 2026")})
    sweeper.poll()
    assert sorted(pid for pid, _ in sweeper.seen) == [200, 300]           # descendants only, never the CLI or a stranger
    monkeypatch.setattr(harness, "process_table", lambda: {200: (1, "Mon Nov 10 11:30:00 2026"), 300: (1, "Mon Nov 10 10:00:02 2026")})
    assert sweeper.kill_survivors() == [300] and killed == [(300, harness.signal.SIGKILL)]      # 200 is another process now


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_fixture_with_a_symlink_is_refused(world, kind):
    fixture = world.root / "fixture"
    if kind == "file":
        (fixture / "link.txt").symlink_to(fixture / "app.py")
    else:
        (fixture / "notes" / "inner").mkdir()
        (fixture / "notes" / "linkdir").symlink_to(fixture / "notes" / "inner")
    r = run(world, keep=False)
    assert r.returncode == 2 and "symlink" in r.stderr
    assert calls(world) == [] and not world.ledger.exists() and _left_in_tmp(world) == []


def test_allow_write_adds_edit_and_write_only_when_declared(world):
    run(world)
    run(world, config="baseline")
    assert not {"Edit", "Write"} & set(_allowed(world, 0)) and not {"Edit", "Write"} & set(_allowed(world, 1))
    spec = json.loads(world.case.read_text())
    spec["allow_write"] = True
    world.case.write_text(json.dumps(spec))
    run(world)
    run(world, config="baseline")
    copy = os.path.realpath(calls(world)[2]["cwd"])
    for index in (2, 3):
        allowed = _allowed(world, index)
        assert "Edit" not in allowed and "Write" not in allowed        # never a bare allow: any path
        assert "Edit(//%s/**)" % copy.lstrip("/") in allowed or "Edit(//%s/**)" % os.path.realpath(calls(world)[index]["cwd"]).lstrip("/") in allowed
        assert any(t.startswith("Write(//") for t in allowed)
    spec["allow_write"] = "yes"
    world.case.write_text(json.dumps(spec))
    assert run(world).returncode == 2 and len(calls(world)) == 4


# --------------------------------------------------------------------------- task 1, fix round 1

def test_allow_write_is_scoped_to_the_copy_and_denied_everywhere_else(world):
    spec = json.loads(world.case.read_text())
    spec["allow_write"] = True
    world.case.write_text(json.dumps(spec))
    assert run(world, env={"HOME": str(world.root / "realhome")}).returncode == 0
    call = calls(world)[0]
    settings, helper = json.loads(call["settings"]), call["helper"]
    deny = settings["permissions"]["deny"]
    copy = os.path.realpath(call["cwd"])
    for tool in ("Edit", "Write"):
        for folder in (str(REPO), os.path.realpath(str(world.root / "realhome")), helper["dir"]):
            assert "%s(//%s/**)" % (tool, folder.lstrip("/")) in deny, (tool, folder)
        assert "%s(**/benchmark/**)" % tool in deny
        allowed = _allowed(world, 0)
        assert "%s(//%s/**)" % (tool, copy.lstrip("/")) in allowed and tool not in allowed
    assert not helper["dir"].startswith(copy + os.sep) and not copy.startswith(helper["dir"] + os.sep)     # the allow cannot reach the helper
    run(world, config="baseline")
    assert "Edit(//%s/**)" % os.path.realpath(calls(world)[1]["cwd"]).lstrip("/") in _allowed(world, 1)


def test_without_allow_write_no_write_rules_are_added(world):
    run(world)
    settings = json.loads(calls(world)[0]["settings"])
    assert not [d for d in settings["permissions"]["deny"] if d.startswith(("Edit(", "Write("))]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="child subreaper is Linux only")
def test_an_orphan_that_escapes_within_one_poll_is_swept_on_linux(world):
    import time
    pidfile = world.root / "orphan.pid"
    pid = None
    try:
        r = run(world, mode="orphan", env={"FAKE_PIDFILE": str(pidfile)})
        assert r.returncode == 0, r.stdout + r.stderr
        pid = int(pidfile.read_text())
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.1)
        assert not _alive(pid)
    finally:
        if pid and _alive(pid):
            os.kill(pid, 9)


def test_a_blind_sweeper_is_recorded_and_warned_about(world):
    r = run(world, env={"QA_TEST_BLIND": "1"})
    assert r.returncode == 0 and records(world)[0]["sweeper_blind"] is True and "sweeper" in r.stderr.lower()
    r = run(world)
    assert records(world)[1]["sweeper_blind"] is False and "sweeper" not in r.stderr.lower()


def test_process_table_falls_back_to_proc_when_ps_cannot_run(monkeypatch):
    harness = _harness_module()
    def broken(*_a, **_k):
        raise FileNotFoundError("ps")
    monkeypatch.setattr(harness.subprocess, "run", broken)
    table = harness.process_table()
    if os.path.isdir("/proc/self"):
        assert os.getpid() in table and isinstance(table[os.getpid()][1], str)
    else:
        assert table == {}


def test_sigterm_right_after_the_helper_is_written_still_deletes_it(world):
    r = run(world, env={"QA_TEST_SIGTERM_AFTER_HELPER": "1"}, keep=False)
    assert r.returncode != 0 and calls(world) == []
    assert _left_in_tmp(world) == []                       # neither the helper directory nor the copy is left


def test_cli_help_and_plugin_probe_get_no_key(world):
    assert run(world).returncode == 0
    seen = [json.loads(line) for line in (world.root / "help.log").read_text().splitlines()]
    assert {tuple(x["argv"][:1]) for x in seen} == {("--help",), ("plugin",)}
    for item in seen:
        assert "ANTHROPIC_API_KEY" not in item["env"] and SECRET not in json.dumps(item["env"])


def test_the_helper_script_names_cat_by_absolute_path_and_refuses_without_one(tmp_path, monkeypatch):
    harness = _harness_module()
    harness.write_key_helper(SECRET, tmp_path / "a")
    line = (tmp_path / "a" / "helper.sh").read_text().splitlines()[1].split()
    assert line[0] == "exec" and os.path.isabs(line[1]) and os.access(line[1], os.X_OK)
    monkeypatch.setattr(harness.shutil, "which", lambda _name: None)
    monkeypatch.setattr(harness.os.path, "isfile", lambda _p: False)
    with pytest.raises(harness.Refused):
        harness.write_key_helper(SECRET, tmp_path / "b")


# -- generated fixtures, kept history, checkout and exclude ---------------------------------------------------------------

# A generator that makes a three-commit repository. ``generate`` writes the final tree only; ``generate_history`` makes the commits.
# A ``mode.txt`` beside it (the folder is never copied) makes it misbehave: ignore the checkout, leave a stray file, write a symlink,
# an answer-key-named file or a ``.claude`` directory.
GENERATOR = textwrap.dedent('''\
    import os, subprocess

    STAGES = [{"app.py": "def f():\\n    return 1\\n", "requirements.md": "one\\n"},
              {"app.py": "def f():\\n    return 2\\n"},
              {"app.py": "def f():\\n    return 3\\n", "notes/extra.md": "three\\n"}]
    MESSAGES = ["add f", "change f", "change f again"]
    HERE = os.path.dirname(os.path.abspath(__file__))

    def _mode():
        path = os.path.join(HERE, "mode.txt")
        return open(path).read().strip() if os.path.exists(path) else ""

    def _tree(directory, upto):
        for stage in STAGES[:upto + 1]:
            for name, text in stage.items():
                path = os.path.join(str(directory), name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").write(text)

    def _extras(directory):
        mode = _mode()
        if mode == "symlink":
            os.symlink("/etc/hosts", os.path.join(str(directory), "link.txt"))
        if mode == "answerkey":
            open(os.path.join(str(directory), "answer-key.json"), "w").write("{}")
        if mode == "claude":
            os.makedirs(os.path.join(str(directory), ".claude"))
            open(os.path.join(str(directory), ".claude", "x.md"), "w").write("x")

    def generate(directory):
        _tree(directory, len(STAGES) - 1)
        _extras(directory)

    def generate_history(directory, checkout=None):
        last = len(STAGES) - 1 if checkout is None or _mode() == "ignore-checkout" else checkout
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="g", GIT_AUTHOR_EMAIL="g@example.invalid",
                   GIT_COMMITTER_NAME="g", GIT_COMMITTER_EMAIL="g@example.invalid")
        def git(*args):
            subprocess.run(["git", "-C", str(directory), "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"] + list(args),
                           check=True, env=env, capture_output=True)
        git("init", "-q")
        git("symbolic-ref", "HEAD", "refs/heads/work")
        for number in range(last + 1):
            _tree(directory, number)
            _extras(directory)
            git("add", "-A")
            git("commit", "-q", "-m", MESSAGES[number])
        if _mode() == "stray":
            open(os.path.join(str(directory), "stray.txt"), "w").write("left over")
''')


def _generated_world(w, mode=None, **fields):
    number = len(list(w.root.glob("generated-*.json")))
    folder = w.root / ("generated-%d" % number)
    folder.mkdir()
    (folder / "generate.py").write_text(GENERATOR)
    if mode:
        (folder / "mode.txt").write_text(mode)
    case = w.root / ("generated-%d.json" % number)
    case.write_text(json.dumps(dict({"id": "gen-1", "prompt": "Review this.", "fixture": str(folder),
                                     "skill": json.loads(w.case.read_text())["skill"]}, **fields)))
    return case


def _copy(w):
    return pathlib.Path(records(w)[-1]["workdir"])


def _subjects(work):
    return subprocess.run(["git", "-C", str(work), "log", "--format=%s"], capture_output=True, text=True).stdout.split("\n")[:-1]


def test_generated_history_is_kept_only_when_declared(world):
    plain = _generated_world(world)
    assert run(world, case=plain, config="baseline").returncode == 0
    work = _copy(world)
    assert listing(work) == ["app.py", "notes/extra.md", "requirements.md"] and not (work / "generate.py").exists()   # the generator is never copied
    assert _subjects(work) == ["initial import"]                         # fresh history, as for every copy
    kept = _generated_world(world, keep_history=True)
    assert run(world, case=kept, config="baseline").returncode == 0
    work = _copy(world)
    assert _subjects(work) == ["change f again", "change f", "add f"]    # the generator's own commits
    assert listing(work) == ["app.py", "notes/extra.md", "requirements.md"] and not (work / "generate.py").exists()
    assert git(work, "remote") == [] and git(work, "tag") == [] and git(work, "for-each-ref", "--format=%(refname)") == ["refs/heads/work"]
    assert git(work, "status", "--porcelain") == []
    off = _generated_world(world, keep_history=False)
    assert run(world, case=off, config="baseline").returncode == 0 and _subjects(_copy(world)) == ["initial import"]


def test_checkout_leaves_head_at_that_commit_and_drops_the_later_ones(world):
    case = _generated_world(world, keep_history=True, checkout=1)
    assert run(world, case=case, config="baseline").returncode == 0
    work = _copy(world)
    assert _subjects(work) == ["change f", "add f"]
    assert (work / "app.py").read_text() == "def f():\n    return 2\n" and not (work / "notes").exists()
    assert len(git(work, "rev-list", "--all")) == 2 and git(work, "for-each-ref", "--format=%(refname)") == ["refs/heads/work"]
    objects = subprocess.run(["git", "-C", str(work), "cat-file", "--batch-all-objects", "--batch-check"], capture_output=True, text=True).stdout
    assert sum(1 for line in objects.splitlines() if line.split()[1] == "commit") == 2      # nothing later is left in the object store
    assert git(work, "status", "--porcelain") == [] and git(work, "symbolic-ref", "HEAD") == ["refs/heads/work"]     # on the branch, not detached
    first = _generated_world(world, keep_history=True, checkout=0)
    assert run(world, case=first, config="baseline").returncode == 0 and _subjects(_copy(world)) == ["add f"]


@pytest.mark.parametrize("fields", [{"keep_history": "yes"}, {"keep_history": 1}, {"checkout": 1}, {"keep_history": True, "checkout": -1},
                                    {"keep_history": True, "checkout": "1"}, {"keep_history": True, "checkout": True},
                                    {"keep_history": True, "checkout": 3}, {"keep_history": True, "exclude": ["requirements.md"]}])
def test_bad_history_fields_are_refused_before_anything_runs(world, fields):
    case = _generated_world(world, **fields)
    r = run(world, case=case, config="baseline", keep=False)
    assert r.returncode == 2 and calls(world) == [] and not world.ledger.exists() and _left_in_tmp(world) == []


def test_keep_history_needs_a_generator(world):
    spec = json.loads(world.case.read_text())
    spec["keep_history"] = True
    world.case.write_text(json.dumps(spec))
    r = run(world, keep=False)
    assert r.returncode == 2 and "generate.py" in r.stderr and calls(world) == [] and _left_in_tmp(world) == []


def test_exclude_removes_files_before_the_history_is_made(world):
    fixture = world.root / "fixture"
    (fixture / "requirements.md").write_text("The rules.\n")
    (fixture / "docs").mkdir()
    (fixture / "docs" / "spec.md").write_text("A spec.\n")
    (fixture / "docs" / "keep.md").write_text("Kept.\n")
    spec = json.loads(world.case.read_text())
    spec["exclude"] = ["requirements.md", "docs/spec.md"]
    world.case.write_text(json.dumps(spec))
    assert run(world, config="baseline").returncode == 0
    work = _copy(world)
    assert "requirements.md" not in listing(work) and "docs/spec.md" not in listing(work) and "docs/keep.md" in listing(work)
    assert git(work, "status", "--porcelain") == []                                                 # not shown as deleted
    assert git(work, "log", "--all", "--name-only", "--format=") == ["app.py", "docs/keep.md"]       # in no commit
    assert subprocess.run(["git", "-C", str(work), "show", "HEAD:requirements.md"], capture_output=True).returncode != 0
    assert "The rules" not in subprocess.run(["git", "-C", str(work), "log", "--all", "-p"], capture_output=True, text=True).stdout


def test_exclude_applies_to_a_generated_fixture_without_history(world):
    case = _generated_world(world, exclude=["requirements.md", "notes"])
    assert run(world, case=case, config="baseline").returncode == 0
    work = _copy(world)
    assert listing(work) == ["app.py"] and git(work, "status", "--porcelain") == [] and _subjects(work) == ["initial import"]
    assert git(work, "log", "--all", "--name-only", "--format=") == ["app.py"]


@pytest.mark.parametrize("value", ["requirements.md", [], [""], [1], ["../x"], ["/etc/hosts"], ["a/../../b"], ["."], ["docs/../.."], ["x\x00y"],
                                   ["missing.md"], {"a": 1}, ["notes/../app.py"], ["x/./../app.py"], ["docs/.."]])
def test_a_bad_exclude_is_refused_before_anything_runs(world, value):
    spec = json.loads(world.case.read_text())
    spec["exclude"] = value
    world.case.write_text(json.dumps(spec))
    r = run(world, keep=False)
    assert r.returncode == 2 and "exclude" in r.stderr and calls(world) == [] and not world.ledger.exists() and _left_in_tmp(world) == []


@pytest.mark.parametrize("mode,fields", [("ignore-checkout", {"keep_history": True, "checkout": 0}), ("stray", {"keep_history": True}),
                                         ("stray", {"keep_history": True, "checkout": 2}), ("symlink", {"keep_history": True}),
                                         ("symlink", {}), ("answerkey", {}), ("answerkey", {"keep_history": True}), ("claude", {})])
def test_a_generator_that_misbehaves_is_refused(world, mode, fields):
    case = _generated_world(world, mode, **fields)
    r = run(world, case=case, config="baseline", keep=False)
    assert r.returncode == 2 and "generat" in r.stderr and calls(world) == [] and not world.ledger.exists() and _left_in_tmp(world) == []


def test_the_documentation_states_the_copy_history_rule_once_with_its_exception():
    text = HARNESS.read_text(encoding="utf-8")
    assert "newly generated one-commit git history, places" not in text          # the opening paragraph names keep_history
    assert max(len(line) for line in text.splitlines()[:12]) <= 140


# -- the copy's final file state and the installed skill's hash (task 14) -------------------------------------------------

def _state_dir(w, index=0):
    return w.out / "runs" / records(w)[index]["run_id"] / "file-state"


def _meta(w, index=0):
    return json.loads((_state_dir(w, index) / "meta.json").read_text())


def _sha(path):
    import hashlib
    return "sha256:" + hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def test_the_file_state_holds_tracked_and_untracked_changes_against_the_base(world):
    (world.root / "fixture" / "old.md").write_text("old\n")
    edits = {"app.py": "def f():\n    return 2\n", "tests/test_new.py": "def test_new():\n    assert True\n", "old.md": None}
    r = run(world, env={"FAKE_EDITS": edits})
    assert r.returncode == 0, r.stdout + r.stderr
    state, work = _state_dir(world), _workdir(world)
    status = (state / "status.txt").read_text().splitlines()
    assert " M app.py" in status and "?? tests/test_new.py" in status and " D old.md" in status
    assert not [line for line in status if ".claude" in line]          # the installed skill is the harness's, not a change
    diff = (state / "diff.patch").read_text()
    assert "+++ b/tests/test_new.py" in diff and "+    return 2" in diff and "--- a/old.md" in diff
    meta = _meta(world)
    assert meta["error"] is None and meta["head_moved"] is False and meta["diff_truncated"] is False
    assert meta["base_commit"] == records(world)[0]["base_commit"] == git(work, "rev-parse", "HEAD")[0]
    rebuilt = world.root / "rebuilt"                                     # the base plus the diff is the copy as the agent left it
    rebuilt.mkdir()
    archive = subprocess.run(["git", "-C", str(work), "archive", meta["base_commit"]], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(rebuilt)], input=archive, check=True)
    subprocess.run(["git", "apply", "--binary", str(state / "diff.patch")], cwd=str(rebuilt), check=True, capture_output=True,
                   env=dict(os.environ, GIT_CEILING_DIRECTORIES=str(world.root)))
    assert listing(rebuilt) == ["app.py", "tests/test_new.py"]
    assert (rebuilt / "app.py").read_text() == edits["app.py"] and (rebuilt / "tests" / "test_new.py").read_text() == edits["tests/test_new.py"]


def test_the_file_state_is_saved_for_baseline_runs_and_leaves_nothing_behind(world):
    r = run(world, config="baseline", keep=False, env={"FAKE_EDITS": {"app.py": "changed\n"}})
    assert r.returncode == 0, r.stderr
    assert " M app.py" in (_state_dir(world) / "status.txt").read_text() and _left_in_tmp(world) == []


def test_a_commit_the_agent_makes_still_shows_against_the_base(world):
    r = run(world, env={"FAKE_EDITS": {"app.py": "changed\n"}, "FAKE_GIT": [["add", "-A"], ["commit", "-qm", "hide it"]]})
    assert r.returncode == 0, r.stderr
    meta = _meta(world)
    assert meta["head_moved"] is True and meta["head_commit"] != meta["base_commit"]
    assert "+changed" in (_state_dir(world) / "diff.patch").read_text()
    assert (_state_dir(world) / "status.txt").read_text() == ""          # the working tree is clean against the new HEAD


def test_the_repository_configuration_the_agent_leaves_runs_nothing(world):
    import shlex
    marker = world.root / "ran"
    touch = "touch %s" % shlex.quote(str(marker))
    config = ("[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tfsmonitor = %s\n\tpager = %s\n"
              "[filter \"x\"]\n\tclean = %s\n\tsmudge = %s\n[diff \"x\"]\n\ttextconv = %s\n\tcommand = %s\n[diff]\n\texternal = %s\n") % ((touch,) * 7)
    edits = {"app.py": "changed\n", "new.txt": "n\n", ".gitattributes": "* filter=x diff=x\n", ".git/config": config}
    r = run(world, env={"FAKE_EDITS": edits})
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    state = _state_dir(world)
    assert "+changed" in (state / "diff.patch").read_text() and "?? new.txt" in (state / "status.txt").read_text().splitlines()
    subprocess.run(["git", "-C", str(_workdir(world)), "diff", "HEAD"], capture_output=True)   # the same configuration, read as it stands,
    assert marker.exists()                                                                       # does run a program: the test shows something


def _hostile_filter(marker):
    """A repository configuration whose content filter runs a program that leaves ``marker``, and the attribute that uses it."""
    import shlex
    touch = "touch %s" % shlex.quote(str(marker))
    config = ("[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
              "[filter \"x\"]\n\tclean = %s\n\tsmudge = %s\n\tprocess = %s\n[user]\n\tagentkey = planted\n") % ((touch,) * 3)
    return {".gitattributes": "* filter=x\n", ".git/config": config}


# Each class of file that sends git from the git directory to another place: the common-directory redirect, object
# alternates (local and over http), grafts, and a git file in place of the directory.
INDIRECTIONS = {".git/commondir": "@CWD@/.git\n", ".git/objects/info/alternates": "@CWD@/.git/objects\n",
                ".git/objects/info/http-alternates": "file://@CWD@/.git/objects\n",
                ".git/info/grafts": "0123456789012345678901234567890123456789\n", ".git": "gitdir: @CWD@/.git-elsewhere\n"}


@pytest.mark.parametrize("target", sorted(INDIRECTIONS))
def test_an_indirection_in_the_git_directory_gives_no_file_state_and_runs_nothing(world, target):
    marker = world.root / "ran"
    edits = dict(_hostile_filter(marker), **{"app.py": "changed\n", target: INDIRECTIONS[target]})
    r = run(world, env={"FAKE_EDITS": edits})
    assert r.returncode == 1 and "file state not saved" in r.stderr and SECRET not in r.stderr
    assert not marker.exists()
    rec = records(world)[0]
    assert rec["status"] == "ok" and [json.loads(line)["event"] for line in world.ledger.read_text().splitlines()] == ["start", "end"]
    state = _state_dir(world)
    assert not (state / "status.txt").exists() and not (state / "diff.patch").exists()
    assert ("indirection" if target != ".git" else "plain directory") in _meta(world)["error"]


def test_a_common_directory_redirect_is_live_without_the_guard_and_dead_with_the_pin(world):
    """The control for the class above: with git's directory alone pointing at a clean copy, the redirect makes git read the
    live configuration and run its program; with the common directory pinned too, nothing runs."""
    harness = _harness_module()
    marker = world.root / "ran"
    edits = dict(_hostile_filter(marker), **{"app.py": "changed\n", ".git/commondir": INDIRECTIONS[".git/commondir"]})
    assert run(world, env={"FAKE_EDITS": edits}).returncode == 1
    work = _workdir(world)
    for pinned in (True, False):
        copy = world.root / ("copy-pinned" if pinned else "copy-unpinned")
        shutil.copytree(str(work / ".git"), str(copy), symlinks=True)
        (copy / "config").write_text(harness._CLEAN_CONFIG)
        where = {"GIT_DIR": str(copy), "GIT_WORK_TREE": str(work)}
        if pinned:
            where.update(GIT_COMMON_DIR=str(copy), GIT_OBJECT_DIRECTORY=str(copy / "objects"))
        where["GIT_INDEX_FILE"] = str(world.root / ("index-" + copy.name))         # staging every file runs its clean filter
        subprocess.run(["git"] + harness._GIT_SAFE_OPTS + ["add", "-A", "--", "."], cwd=str(work),
                       env=harness._safe_git_env(where), capture_output=True)
        assert marker.exists() is (not pinned)


def test_the_snapshot_reads_no_configuration_from_the_copy(world):
    marker = world.root / "ran"
    edits = dict(_hostile_filter(marker), **{"app.py": "changed\n"})
    assert run(world, env={"FAKE_EDITS": edits}).returncode == 0
    assert not marker.exists()
    entries = _meta(world)["git_config"]
    assert entries and all(e.startswith(("command line ", "harness ")) for e in entries)
    assert sorted(e.split(" ", 1)[1] for e in entries if e.startswith("harness ")) == \
        ["core.bare", "core.filemode", "core.repositoryformatversion"]
    assert not [e for e in entries if "filter." in e or "user." in e]


def test_a_configuration_origin_other_than_the_harness_is_a_problem(tmp_path):
    harness = _harness_module()
    own = tmp_path / "git" / "config"
    good = b"command line:\0core.fsmonitor\nfalse\0file:" + str(own).encode() + b"\0core.bare\nfalse\0"
    assert harness._config_problem(good, own) == (["command line core.fsmonitor", "harness core.bare"], None)
    for bad in (good + b"file:/elsewhere/config\0filter.x.clean\ntouch\0",
                good + b"file:" + str(own).encode() + b"\0filter.x.clean\ntouch\0",
                good + b"blob:abc\0core.editor\nvi\0"):
        assert harness._config_problem(bad, own)[1]


def test_replace_refs_change_nothing_in_the_diff(world):
    git_cmds = [["add", "-A"], ["commit", "-qm", "the change"], ["replace", "HEAD~1", "HEAD"]]
    assert run(world, env={"FAKE_EDITS": {"app.py": "changed\n"}, "FAKE_GIT": git_cmds}).returncode == 0
    assert "+changed" in (_state_dir(world) / "diff.patch").read_text()
    live = subprocess.run(["git", "-C", str(_workdir(world)), "diff", "HEAD~1", "HEAD"], capture_output=True, text=True)
    assert live.returncode == 0 and live.stdout == ""          # read as it stands, the replace ref hides the change


def test_the_fixture_tree_is_recorded_in_the_end_record_and_the_evidence(world):
    assert run(world).returncode == 0
    work, meta = _workdir(world), _meta(world)
    tree = git(work, "rev-parse", meta["base_commit"] + "^{tree}")[0]
    assert meta["fixture_tree"] == records(world)[0]["fixture_tree"] == tree and len(tree) == 40


KERNEL_KEY_A = bytes(range(200, 232))                        # not valid UTF-8, as the kernel's random keys mostly are
KERNEL_KEY_R = b"\xff\x00kernel-redaction-key-bytes\x80\x81\x82"


def _kernel_key_forms(raw):
    import base64
    shown = raw.decode("utf-8", "replace")
    forms = {raw.hex(), base64.b64encode(raw).decode()[:-4], shown, json.dumps(shown)[1:-1], json.dumps(shown, ensure_ascii=False)[1:-1]}
    return [f for f in forms if len(f.encode()) >= 16]


def test_the_copys_kernel_keys_are_redacted_everywhere_they_are_saved(world):
    import base64
    _setup_case(world, "import os\nos.makedirs('.qa', exist_ok=True)\n"
                       "open('.qa/authority.key', 'wb').write(%r)\nopen('.qa/redaction.key', 'wb').write(%r)\n"
                       % (KERNEL_KEY_A, KERNEL_KEY_R))
    late = "agent-made-key-0123456789abcdef"
    edits = {".qa/ledger/events.jsonl": "seen %s\n" % KERNEL_KEY_A.hex(), "leak.txt": base64.b64encode(KERNEL_KEY_R).decode() + "\n",
             ".qa/authority.key": None,                                   # gone by the end: the key read after the setup counts
             ".qa/redaction.key": late, "late.txt": "late %s\n" % late}  # made by the agent: the key read at the end counts
    r = run(world, env={"FAKE_CAT": [".qa/authority.key", ".qa/redaction.key"], "FAKE_EDITS": edits})
    assert r.returncode == 0, r.stderr
    run_dir = world.out / "runs" / records(world)[0]["run_id"]
    saved = [p for p in run_dir.rglob("*") if p.is_file()]
    forms = _kernel_key_forms(KERNEL_KEY_A) + _kernel_key_forms(KERNEL_KEY_R) + [late]
    for path in saved:
        data = path.read_bytes()
        assert KERNEL_KEY_A not in data and KERNEL_KEY_R not in data, path
        text = data.decode("utf-8", "replace")
        assert not [f for f in forms if f in text], path
    for rel in ("stdout.txt", "stderr.txt", "qa-state/ledger/events.jsonl"):
        assert "[REDACTED:kernel-key]" in (run_dir / rel).read_text(), rel
    held = [".qa/ledger/events.jsonl", ".qa/redaction.key", "late.txt", "leak.txt"]    # each holds a key in some form
    assert _meta(world)["redacted_files"] == held
    diff = (run_dir / "file-state" / "diff.patch").read_text()
    assert not [p for p in held if "b/" + p in diff]


def _nested_repository(marker, committed):
    """An agent's moves that leave a populated nested repository registered as a gitlink (staged, or committed too), whose
    own configuration names a content filter that runs a program leaving ``marker``, with its file stat-dirty so that a
    status taken inside it hashes the file again."""
    git_cmds = [["-C", "sub", "init", "-q"], ["-C", "sub", "add", "-A"], ["-C", "sub", "commit", "-qm", "s"], ["add", "sub"]]
    if committed:
        git_cmds.append(["commit", "-qm", "gitlink"])
    hostile = _hostile_filter(marker)
    after = ("import os, time\nopen('sub/.git/config', 'w').write(%r)\nopen('sub/.gitattributes', 'w').write(%r)\n"
             "time.sleep(1.1)\nos.utime('sub/s.txt')\n") % (hostile[".git/config"], hostile[".gitattributes"])
    return {"FAKE_EDITS": {"app.py": "changed\n", "sub/s.txt": "aaa\n"}, "FAKE_GIT": git_cmds, "FAKE_PY": [after]}


@pytest.mark.parametrize("committed", [False, True], ids=["staged", "committed"])
def test_a_nested_repository_is_recorded_as_a_gitlink_and_never_entered(world, committed):
    harness = _harness_module()
    marker = world.root / "ran"
    r = run(world, env=_nested_repository(marker, committed))
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    state, meta, work = _state_dir(world), _meta(world), _workdir(world)
    assert " M app.py" in (state / "status.txt").read_text().splitlines()
    assert "+changed" in (state / "diff.patch").read_text()
    link = [{"commit": git(work / "sub", "rev-parse", "HEAD")[0], "path": "sub"}]
    assert meta["gitlinks"] == {"head": link if committed else [], "index": link, "work_tree": link}
    # The control: the same status, taken with the copy pinned as the snapshot pins it but without the options that keep
    # git out of nested repositories, enters the nested one and runs the program its configuration names.
    copy = world.root / "copy"
    shutil.copytree(str(work / ".git"), str(copy), symlinks=True)
    (copy / "config").write_text(harness._CLEAN_CONFIG)
    where = {"GIT_DIR": str(copy), "GIT_COMMON_DIR": str(copy), "GIT_OBJECT_DIRECTORY": str(copy / "objects"),
             "GIT_WORK_TREE": str(work)}
    opts = harness._GIT_SAFE_OPTS
    kept = [part for pair in zip(opts[::2], opts[1::2]) if "ubmodule" not in pair[1] for part in pair]
    subprocess.run(["git"] + kept + ["status", "--porcelain=v1", "-uall", "--no-renames"], cwd=str(work),
                   env=harness._safe_git_env(where), capture_output=True)
    assert marker.exists()


def test_a_file_holding_a_kernel_key_is_left_out_of_the_diff_and_named(world):
    """Git writes a file holding a NUL as a binary patch, in which no byte of the key shows as it is: such a file, and any
    other whose content on either side holds a kernel key in some form, is left out of the diff and named instead."""
    script = ("import os, subprocess\nos.makedirs('.qa', exist_ok=True)\nkey = %r\nopen('.qa/authority.key', 'wb').write(key)\n"
              "open('old.bin', 'wb').write(b'\\0old' + key)\n"
              "for args in (['add', 'old.bin'], ['commit', '-qm', 'setup']):\n"
              "    subprocess.run(['git', '-c', 'user.name=a', '-c', 'user.email=a@a', '-c', 'commit.gpgsign=false'] + args,"
              " check=True, capture_output=True)\n") % KERNEL_KEY_A
    _setup_case(world, script)
    after = ("key = open('.qa/authority.key', 'rb').read()\nopen('blob.bin', 'wb').write(b'\\0head' + key + b'\\0tail')\n"
             "open('hex.bin', 'wb').write(b'\\0' + key.hex().encode())\n")
    r = run(world, env={"FAKE_EDITS": {"plain.txt": "kept\n", "old.bin": None}, "FAKE_PY": [after]})
    assert r.returncode == 0, r.stderr
    run_dir, meta, work = world.out / "runs" / records(world)[0]["run_id"], _meta(world), _workdir(world)
    assert meta["redacted_files"] == [".qa/authority.key", "blob.bin", "hex.bin", "old.bin"]
    diff = (run_dir / "file-state" / "diff.patch").read_text()
    assert "+++ b/plain.txt" in diff and not [p for p in meta["redacted_files"] if "/" + p in diff]
    for path in [p for p in run_dir.rglob("*") if p.is_file()]:
        data = path.read_bytes()
        assert KERNEL_KEY_A not in data and not [f for f in _kernel_key_forms(KERNEL_KEY_A) if f in data.decode("utf-8", "replace")]
    # The control: the diff git writes for such a file is a binary patch that holds no form of the key a byte match finds.
    index = dict(os.environ, GIT_INDEX_FILE=str(world.root / "control-index"))
    subprocess.run(["git", "-C", str(work), "read-tree", meta["base_commit"]], env=index, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "add", "-A", "--", "blob.bin"], env=index, check=True, capture_output=True)
    shown = subprocess.run(["git", "-C", str(work), "diff", "--cached", "--binary", meta["base_commit"], "--", "blob.bin"], env=index,
                           check=True, capture_output=True).stdout
    assert b"GIT binary patch" in shown and KERNEL_KEY_A not in shown and KERNEL_KEY_A.hex().encode() not in shown


def test_a_refused_setup_keeps_its_output_without_the_copys_kernel_keys(world):
    _setup_case(world, "import base64, os, sys\nos.makedirs('.qa', exist_ok=True)\nkey = %r\n"
                       "open('.qa/authority.key', 'wb').write(key)\nsys.stdout.buffer.write(key + b'\\n')\nsys.stdout.flush()\n"
                       "print(key.hex())\nprint(base64.b64encode(key).decode(), file=sys.stderr)\nsys.exit(4)\n" % KERNEL_KEY_A)
    r = run(world)
    assert r.returncode == 3 and "setup script exited 4" in (r.stdout + r.stderr)
    assert calls(world) == [] and records(world) == []
    saved = next((world.out / "runs").rglob("setup.txt")).read_bytes()
    text = saved.decode("utf-8", "replace")
    assert KERNEL_KEY_A not in saved and not [f for f in _kernel_key_forms(KERNEL_KEY_A) if f in text]
    assert text.count("[REDACTED:kernel-key]") >= 3


@pytest.mark.parametrize("target", [".git/config", ".git/objects", ".git"])
def test_a_git_directory_holding_a_symlink_gives_no_file_state_and_is_a_problem(world, target):
    elsewhere = world.root / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("untouched\n")
    r = run(world, env={"FAKE_EDITS": {"app.py": "changed\n"}, "FAKE_LINKS": {target: str(elsewhere)}})
    assert r.returncode == 1 and "file state not saved" in r.stderr and SECRET not in r.stderr
    rec = records(world)[0]
    assert rec["status"] == "ok" and [json.loads(line)["event"] for line in world.ledger.read_text().splitlines()] == ["start", "end"]
    state = _state_dir(world)
    assert not (state / "status.txt").exists() and not (state / "diff.patch").exists() and _meta(world)["error"]
    assert listing(elsewhere) == ["keep.txt"] and (elsewhere / "keep.txt").read_text() == "untouched\n"


def test_the_installed_skill_hash_is_in_the_end_record_and_the_evidence(world):
    skill = pathlib.Path(json.loads(world.case.read_text())["skill"]) / "SKILL.md"
    expected = {"qa-demo": _sha(skill)}
    assert run(world, env={"FAKE_EDITS": {".claude/skills/qa-demo/SKILL.md": "changed by the agent\n"}}).returncode == 0
    assert records(world)[0]["skill_sha256"] == expected == _meta(world)["skill_sha256"]     # what was installed, not what is left
    assert run(world, config="baseline").returncode == 0
    assert records(world)[1]["skill_sha256"] == {} == _meta(world, 1)["skill_sha256"]


def test_the_file_state_is_redacted(world):
    assert run(world, env={"FAKE_EDITS": {"leak.txt": "key @KEY@\n"}}).returncode == 0
    text = (_state_dir(world) / "diff.patch").read_text()
    assert SECRET not in text and "[REDACTED:ANTHROPIC_API_KEY]" in text


def test_a_diff_past_the_limit_is_cut_and_says_so(world):
    r = run(world, env={"FAKE_EDITS": {"big.txt": "x" * 5000 + "\n"}, "QA_TEST_FILE_STATE_LIMIT": "1000"})
    assert r.returncode == 0, r.stderr
    assert _meta(world)["diff_truncated"] is True and len((_state_dir(world) / "diff.patch").read_bytes()) <= 1000


def test_the_end_line_comes_before_the_file_state_and_a_failed_capture_is_a_problem(world, monkeypatch):
    repo = _harness_copy(world, monkeypatch)
    text = (repo / "tools" / "run_skill_eval.py").read_text()
    assert "    git_dir = workdir / \".git\"\n" in text
    (repo / "tools" / "run_skill_eval.py").write_text(text.replace(
        "    git_dir = workdir / \".git\"\n", "    raise PermissionError('no')\n", 1))
    r = run(world, env={"FAKE_QA": True})
    assert r.returncode == 1 and "file state not saved" in r.stderr
    rec = records(world)[0]
    assert rec["status"] == "ok" and (world.out / "runs" / rec["run_id"] / "qa-state" / "config.json").is_file()
    assert [json.loads(line)["event"] for line in world.ledger.read_text().splitlines()] == ["start", "end"]


def test_qa_state_keeps_the_patches_under_work_and_nothing_else_of_it(world):
    edits = {".qa/work/patches/tc-001.diff": "+++ b/tests/test_x.py\n", ".qa/work/wt-abc/app.py": "x\n", ".qa/work/scratch.txt": "w",
             ".qa/evidence/work/x.txt": "deep", ".qa/work/patches/sub/two.diff": "2\n"}
    r = run(world, env={"FAKE_EDITS": edits, "FAKE_LINKS": {".qa/work/patches/link.diff": "/etc/hosts"}})
    assert r.returncode == 0, r.stderr
    saved = world.out / "runs" / records(world)[0]["run_id"] / "qa-state"
    files = sorted(str(p.relative_to(saved)) for p in saved.rglob("*") if p.is_file() or p.is_symlink())
    assert files == ["work/patches/sub/two.diff", "work/patches/tc-001.diff"]
