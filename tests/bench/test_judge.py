"""Tests for benchmark/harness/judge.py, driven by a fake ``claude`` executable (no model, no network, nothing paid).

The key in these tests is a made-up test value; no real key is read."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import textwrap

import pytest

from helpers import REPO

JUDGE = REPO / "benchmark" / "harness" / "judge.py"
SECRET = "sk-test-judge-0123456789-not-a-real-key"
FLAGS = ("-p", "--output-format", "--model", "--bare", "--tools", "--disable-slash-commands", "--setting-sources",
         "--strict-mcp-config", "--mcp-config", "--settings", "--no-session-persistence", "--system-prompt", "--max-budget-usd")

# The fake CLI reads its behaviour from claude.cfg beside it (the judge's environment is minimal): LOG (one JSON line per
# call), RESULT (the result text, where KEY stands for what the key helper prints), COST (None leaves the cost out), HELP (the
# flags --help lists) and EXIT.
FAKE = textwrap.dedent('''\
    #!python
    import json, os, subprocess, sys
    argv = sys.argv[1:]
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude.cfg")))
    if argv[:1] == ["--help"]:
        for flag in cfg["HELP"]:
            print("  " + flag)
        sys.exit(0)
    stdin = sys.stdin.read()
    settings = json.load(open(argv[argv.index("--settings") + 1]))
    mcp = json.load(open(argv[argv.index("--mcp-config") + 1]))
    helped = subprocess.run(settings["apiKeyHelper"], shell=True, capture_output=True, text=True).stdout
    with open(cfg["LOG"], "a") as fh:
        fh.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": stdin, "cwd": os.getcwd(), "settings": settings,
                             "mcp": mcp, "helped": helped}) + "\\n")
    event = {"type": "result", "subtype": "success", "is_error": False, "result": cfg["RESULT"].replace("KEY", helped),
             "duration_ms": 12, "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 3,
                                          "cache_creation_input_tokens": 4}}
    if cfg["COST"] is not None:
        event["total_cost_usd"] = cfg["COST"]
    print(json.dumps(event))
    sys.exit(cfg.get("EXIT", 0))
''')


def judge_module():
    """judge.py, loaded by path. It runs the CLI through the harness's ``execute``, which on Linux makes its caller a child
    subreaper; here the caller is the pytest process itself, where orphans of later tests would then linger as zombies
    instead of being reaped by init, so this copy of the harness never becomes one."""
    spec = importlib.util.spec_from_file_location("judge_under_test", str(JUDGE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._harness.become_subreaper = lambda: False
    return module


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE.replace("#!python", "#!" + sys.executable, 1))
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", os.defpath))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    class Fake:
        log = tmp_path / "fake.log"
        out = tmp_path / "stage"
        ledger = tmp_path / "ledger" / "costs.jsonl"

        def set(self, result='{"claims": []}', cost=0.03, help_flags=FLAGS, exit=0):
            (bin_dir / "claude.cfg").write_text(json.dumps({"LOG": str(self.log), "RESULT": result, "COST": cost,
                                                            "HELP": list(help_flags), "EXIT": exit}))

        def calls(self):
            return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    f = Fake()
    f.set()
    return f


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def ask(module, fake, subject="run1", **over):
    given = dict(model="test-judge", budget_usd=1.0, ledger=fake.ledger, out=fake.out, subject=subject)
    given.update(over)
    return module.run_judge("Answer with one JSON object.", {"final_message": "All good."}, **given)


def test_judge_records_cost_and_refuses_to_guess(fake, tmp_path, monkeypatch):
    module = judge_module()
    done = ask(module, fake)
    assert done["answer"] == {"claims": []} and done["status"] == "ok"
    assert (done["cost_usd"], done["cost_unknown"], done["model"]) == (0.03, False, "test-judge")
    ledger = lines(fake.ledger)
    assert [(x["event"], x["engine"]) for x in ledger] == [("start", "judge"), ("end", "judge")]
    assert ledger[0]["run_id"] == ledger[1]["run_id"] == done["judge_id"]
    end = ledger[1]
    assert (end["cost_usd"], end["cost_unknown"], end["status"], end["config"]) == (0.03, False, "ok", "judge")
    assert end["case_id"] == "judge:run1" and end["subject"] == "run1" and end["max_budget_usd"] == "1.0000"
    assert end["tokens"] == {"input": 100, "output": 20, "cache_read": 3, "cache_creation": 4}
    assert lines(fake.out / "costs.jsonl") == [end]           # the stage's costs file holds the judge's end line too
    raw = fake.out / done["raw_path"]
    assert done["raw_path"] == "grades/run1.judge-raw.txt" and json.loads(raw.read_text())["result"] == '{"claims": []}'
    assert "All good." in (fake.out / "grades" / "run1.judge-prompt.txt").read_text()

    # One fenced JSON object is read; prose, two objects, a list or a fence holding no object is never guessed at.
    fake.set(result='Here it is:\n```json\n{"claims": [{"text": "x"}]}\n```\n')
    assert ask(module, fake, "run2")["answer"] == {"claims": [{"text": "x"}]}
    for number, said in enumerate(['The claims look backed to me.', '{"claims": []} {"claims": []}', '[1, 2]',
                                   '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```', '```json\nnot json\n```']):
        fake.set(result=said)
        done = ask(module, fake, "bad%d" % number)
        assert done["answer"] == {"error": "judge_unparseable"}, said
        assert done["cost_usd"] == 0.03                       # what it cost is still recorded
    # A failed call is an error answer, not a guess; a cost the CLI did not report is unknown, never 0.
    fake.set(exit=1)
    done = ask(module, fake, "failed")
    assert done["status"] == "failed" and done["answer"] == {"error": "judge_failed"} and done["cost_usd"] == 0.03
    fake.set(cost=None)
    done = ask(module, fake, "nocost")
    assert (done["cost_usd"], done["cost_unknown"]) == (None, True)
    assert lines(fake.ledger)[-1]["cost_unknown"] is True and lines(fake.ledger)[-1]["cost_usd"] is None
    assert len(fake.calls()) == 9 and len(lines(fake.ledger)) == 18

    # Refusals call nothing and write no ledger line: an unknown cost already in the ledger, no key, a flag the installed
    # CLI does not list, and a ledger whose budget is spent.
    fake.set()
    with pytest.raises(module.JudgeRefused):
        ask(module, fake, "after-unknown")
    clean = tmp_path / "clean" / "costs.jsonl"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(module.JudgeRefused):
        ask(module, fake, "no-key", ledger=clean)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    fake.set(help_flags=[f for f in FLAGS if f != "--bare"])
    with pytest.raises(module.JudgeRefused):
        ask(module, fake, "no-flag", ledger=clean)
    fake.set()
    spent = tmp_path / "spent" / "costs.jsonl"
    spent.parent.mkdir()
    spent.write_text(json.dumps({"event": "start", "run_id": "r0"}) + "\n" + json.dumps(
        {"event": "end", "run_id": "r0", "cost_usd": 1.0, "cost_unknown": False}) + "\n")
    with pytest.raises(module.JudgeRefused):
        ask(module, fake, "spent", ledger=spent, budget_usd=1.0)
    assert len(lines(fake.ledger)) == 18 and len(lines(spent)) == 2 and not clean.exists() and len(fake.calls()) == 9


def test_judge_runs_isolated_with_no_tools_and_the_key_only_through_its_helper(fake, tmp_path):
    """Ruling 3: a fresh environment of exactly the agent's names, a temporary home and configuration (removed afterwards),
    the key only through apiKeyHelper, no tools, no plugins, no MCP servers, slash commands off, and the raw output redacted."""
    module = judge_module()
    fake.set(result='{"claims": [], "note": "KEY"}')
    done = ask(module, fake, stage_remaining_usd=0.4)
    call = fake.calls()[0]
    assert set(call["env"]) - {"PWD", "SHLVL", "_", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {
        "PATH", "HOME", "CLAUDE_CONFIG_DIR", "DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}
    assert (call["env"]["DISABLE_TELEMETRY"], call["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]) == ("1", "1")
    assert SECRET not in json.dumps(call["env"]) and SECRET not in json.dumps(call["argv"]) and SECRET not in call["stdin"]
    assert call["helped"] == SECRET                            # the key reaches the CLI through its helper only
    assert set(call["settings"]) == {"apiKeyHelper"} and call["mcp"] == {"mcpServers": {}}
    home, config = call["env"]["HOME"], call["env"]["CLAUDE_CONFIG_DIR"]
    assert call["cwd"] == os.path.realpath(home) or call["cwd"] == home
    for made in (home, config, os.path.dirname(call["settings"]["apiKeyHelper"].strip("'"))):
        assert not os.path.exists(made), made                   # temporary, and removed once the call is done
    assert os.path.realpath(home) != os.path.realpath(os.path.expanduser("~"))
    argv = call["argv"]
    assert argv[:4] == ["-p", "--output-format", "json", "--model"] and argv[4] == "test-judge"
    assert argv[argv.index("--tools") + 1] == ""               # no tools at all
    for flag in ("--bare", "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert not {"--plugin-dir", "--allowedTools", "--verbose", "--add-dir"} & set(argv)
    assert argv[argv.index("--max-budget-usd") + 1] == "0.4000"     # the stage's remainder is smaller than the ledger's
    assert done["answer"] == {"claims": [], "note": "[REDACTED:ANTHROPIC_API_KEY]"}
    saved = "".join(p.read_text() for p in (fake.out / "grades").iterdir())
    assert SECRET not in saved and SECRET not in (fake.out / "costs.jsonl").read_text()
    assert SECRET not in fake.ledger.read_text()
