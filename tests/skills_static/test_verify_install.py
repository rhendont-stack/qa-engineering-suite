"""L0 tests for tools/verify_install.py with a fake ``claude`` (the real CLI runs on the Mac only)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from helpers import REPO

TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))
import verify_install  # noqa: E402

FAKE = '''#!{python}
import json, os, pathlib, sys
INVENTORY = {inventory}
LOG = pathlib.Path({log!r})
args = sys.argv[1:]
with LOG.open("a") as handle:
    handle.write(json.dumps({{"args": args, "env": dict(os.environ)}}) + "\\n")
if args[:2] == ["plugin", "validate"]:
    print("Validating marketplace manifest\\n\\n\\u2714 Validation passed")
elif args[:3] == ["plugin", "marketplace", "add"]:
    print("Successfully added marketplace: qa-engineering-suite")
elif args[:2] == ["plugin", "install"]:
    name = args[2].split("@")[0]
    target = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"], "plugins", "cache", "qa-engineering-suite", name, "0.1.0-dev.1")
    target.mkdir(parents=True)
    (target / "payload").write_bytes(b"x" * 100)
    print("Successfully installed plugin: " + args[2])
elif args[:2] == ["plugin", "details"]:
    name = args[2].split("@")[0]
    skills, agents, hooks = INVENTORY[name]
    print(name + " 0.1.0-dev.1\\n  Source: " + args[2] + "\\n\\nComponent inventory")
    print("  Skills (%d)  %s" % (len(skills), ", ".join(skills)))
    print("  Agents (%d)  %s" % (len(agents), ", ".join(agents)))
    print("  Hooks (%d)" % hooks)
    print("  MCP servers (0)\\n  LSP servers (0)")
    if (LOG.parent / "details-fails").exists():
        sys.exit(1)
else:
    sys.exit(9)
'''


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A small git repository: marketplace with one bundle, a catalog of two skills, one agent, the guard manifest."""
    root = tmp_path / "repo"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / "plugins" / "qa-command-guard" / ".claude-plugin").mkdir(parents=True)
    (root / "agents").mkdir()
    (root / "agents" / "qa-auditor.md").write_text("---\nname: qa-auditor\n---\n")
    for name in ("qa-one", "qa-two"):
        (root / "skills" / name).mkdir(parents=True)
        (root / "skills" / name / "SKILL.md").write_text("---\nname: %s\n---\n" % name)
    (root / "catalog.json").write_text(json.dumps({"version": 1, "skills": ["qa-one", "qa-two"], "responsibilities": []}))
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "qa-engineering-suite", "plugins": [
        {"name": "full", "source": "./"},
        {"name": "part", "source": "./", "strict": False, "skills": ["./skills/qa-one"], "agents": ["./agents/qa-auditor.md"]},
        {"name": "qa-command-guard", "source": "./plugins/qa-command-guard"}]}))
    (root / "plugins" / "qa-command-guard" / ".claude-plugin" / "plugin.json").write_text("{}")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "x")
    return root


def fake_claude(tmp_path, inventory):
    path = tmp_path / "fake-claude"
    log = tmp_path / "calls.jsonl"
    path.write_text(FAKE.format(python=sys.executable, inventory=repr(inventory), log=str(log)))
    path.chmod(0o755)
    return path, log


def run(repo, tmp_path, claude, *extra, env=None):
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-I", str(TOOLS / "verify_install.py"), "--out", str(out), "--claude", str(claude),
                        "--root", str(repo), *extra], capture_output=True, text=True, timeout=120, env=env)
    return r, out


GOOD = {"full": (["qa-one", "qa-two"], ["qa-auditor"], 0), "part": (["qa-one"], ["qa-auditor"], 0),
        "qa-command-guard": ([], [], 0)}


def test_expected_inventory_follows_the_manifest_and_the_catalog(repo):
    assert verify_install.expected_inventory(repo) == {
        "full": {"skills": ["qa-one", "qa-two"], "agents": ["qa-auditor"], "hooks": 0},
        "part": {"skills": ["qa-one"], "agents": ["qa-auditor"], "hooks": 0},
        "qa-command-guard": {"skills": [], "agents": [], "hooks": 0}}


def test_expected_hooks_count_the_events_with_a_handler(repo):
    hooks = repo / "plugins" / "qa-command-guard" / "hooks"
    hooks.mkdir()
    handler = {"type": "command", "command": "x"}

    def expect(events):
        (hooks / "hooks.json").write_text(json.dumps({"hooks": events}))
        return verify_install.expected_inventory(repo)["qa-command-guard"]["hooks"]

    assert expect({"PreToolUse": [{"matcher": "Bash", "hooks": [handler]}]}) == 1
    # the CLI prints "Hooks (2)  PreToolUse, SessionStart" for this: one count per event, not per group or handler
    assert expect({"PreToolUse": [{"matcher": "Bash", "hooks": [handler, handler]}, {"matcher": "Write", "hooks": [handler]}],
                   "SessionStart": [{"hooks": [handler]}]}) == 2
    assert expect({"PreToolUse": [{"matcher": "Bash", "hooks": []}], "Stop": []}) == 0
    assert expect([]) == 0


def test_parse_details_reads_the_component_inventory():
    text = "p 1\n  Source: p@m\n\nComponent inventory\n  Skills (2)  a, b\n  Agents (0)\n  Hooks (1)\n  MCP servers (0)\n"
    assert verify_install.parse_details(text) == {"skills": ["a", "b"], "agents": [], "hooks": 1}
    assert verify_install.parse_details("nothing here") is None


def test_verify_install_compares_inventories(repo, tmp_path):
    claude, log = fake_claude(tmp_path, GOOD)
    r, out = run(repo, tmp_path, claude)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads((out / "install.json").read_text())
    assert doc["ok"] is True and len(doc["commit"]) == 40
    assert set(doc["entries"]) == {"full", "part", "qa-command-guard"} and all(e["installed"] for e in doc["entries"].values())
    assert doc["entries"]["part"]["skills"] == 1 and doc["entries"]["full"]["size_bytes"] == 100
    assert all((out / v["output_path"]).is_file() for v in doc["validate"].values()) and len(doc["validate"]) == 2
    assert all((out / e["details_path"]).is_file() for e in doc["entries"].values())
    assert json.loads(r.stdout)["ok"] is True
    # one skill missing from an entry's inventory
    claude, _ = fake_claude(tmp_path, dict(GOOD, full=(["qa-one"], ["qa-auditor"], 0)))
    shutil.rmtree(out)
    r, out = run(repo, tmp_path, claude)
    doc = json.loads((out / "install.json").read_text())
    assert r.returncode == 1 and doc["ok"] is False and doc["entries"]["full"]["skills"] == 1


def test_verify_install_never_uses_the_callers_config(repo, tmp_path):
    claude, log = fake_claude(tmp_path, GOOD)
    env = dict(os.environ, ANTHROPIC_API_KEY="dummy-not-a-key", CLAUDE_CONFIG_DIR=str(tmp_path / "callers"),
               HOME=str(tmp_path / "callers-home"), CLAUDE_CODE_OAUTH_TOKEN="dummy")
    r, out = run(repo, tmp_path, claude, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 2 + 1 + 2 * 3
    for call in calls:
        # LC_CTYPE is set by the fake's own interpreter (locale coercion), not by the tool
        assert set(call["env"]) - {"LC_CTYPE"} == {"PATH", "HOME", "CLAUDE_CONFIG_DIR", "DISABLE_TELEMETRY",
                                                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}
        assert call["env"]["DISABLE_TELEMETRY"] == "1" and call["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert call["env"]["HOME"] != str(tmp_path / "callers-home") and call["env"]["CLAUDE_CONFIG_DIR"] != str(tmp_path / "callers")
        assert call["env"]["HOME"].startswith(call["env"]["CLAUDE_CONFIG_DIR"].rsplit("/", 1)[0])
    assert len({c["env"]["HOME"] for c in calls}) == 1
    assert not os.path.exists(calls[0]["env"]["HOME"])  # the temporary directory is removed afterwards
    assert not (tmp_path / "callers").exists() and not (tmp_path / "callers-home").exists()


def test_verify_install_refuses_a_missing_claude_and_bad_usage(repo, tmp_path):
    r, _ = run(repo, tmp_path, tmp_path / "no-such-claude")
    assert r.returncode == 3
    assert subprocess.run([sys.executable, "-I", str(TOOLS / "verify_install.py")], capture_output=True).returncode == 2


def test_a_failing_details_call_is_not_ok_even_if_it_printed_the_inventory(repo, tmp_path):
    claude, _ = fake_claude(tmp_path, GOOD)
    (tmp_path / "details-fails").write_text("")
    r, out = run(repo, tmp_path, claude)
    doc = json.loads((out / "install.json").read_text())
    assert r.returncode == 1 and doc["ok"] is False
    assert all(e["installed"] and not e["matches_expected"] and e["skills"] is None for e in doc["entries"].values())


@pytest.mark.parametrize("damage", ["no-source", "no-plugins-key", "plugin-not-an-object", "catalog-without-skills"])
def test_a_malformed_marketplace_is_refused_without_a_traceback(repo, tmp_path, damage):
    market = repo / ".claude-plugin" / "marketplace.json"
    doc = json.loads(market.read_text())
    if damage == "no-source":
        del doc["plugins"][0]["source"]
    elif damage == "no-plugins-key":
        del doc["plugins"]
    elif damage == "plugin-not-an-object":
        doc["plugins"][0] = "qa-one"
    else:
        (repo / "catalog.json").write_text("{}")
    market.write_text(json.dumps(doc))
    git(repo, "commit", "-qam", "damage")
    claude, _ = fake_claude(tmp_path, GOOD)
    r, _ = run(repo, tmp_path, claude)
    assert r.returncode == 3 and "refused" in r.stdout and "malformed" in r.stdout and "Traceback" not in r.stdout + r.stderr


def test_saved_output_hides_both_forms_of_the_temporary_directory(tmp_path):
    real = tmp_path / "real"
    (real / "home").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    script = tmp_path / "say-paths"
    script.write_text("#!%s\nimport os\nhome = os.environ['HOME']\nprint(home, os.path.realpath(home), sep='\\n')\n" % sys.executable)
    script.chmod(0o755)
    code, text = verify_install.run_claude(str(script), [], link)
    assert code == 0 and text == "<TMP>/home\n<TMP>/home\n", text


@pytest.mark.skipif(not hasattr(__import__("tarfile"), "data_filter"), reason="extraction filters need Python 3.12+ or a security release")
def test_the_export_refuses_a_link_that_points_outside(repo, tmp_path):
    (repo / "outside-link").symlink_to("/etc")
    git(repo, "add", "outside-link")
    git(repo, "commit", "-qm", "link")
    claude, _ = fake_claude(tmp_path, GOOD)
    r, out = run(repo, tmp_path, claude)
    assert r.returncode == 3 and "refused" in r.stdout and "Traceback" not in r.stdout + r.stderr
