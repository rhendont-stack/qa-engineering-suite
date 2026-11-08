from __future__ import annotations

import re
import stat
import threading

import pytest

from qa import schema, workspace
from qa.errors import QaError


def test_init_creates_layout(ws):
    assert all((ws.qa_dir / d).is_dir() for d in ["ledger", "evidence", "state", "reports", "work"])
    assert (ws.qa_dir / ".gitignore").read_text() == "*\n!.gitignore\n!config.json\n"
    key = ws.qa_dir / "redaction.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600 and len(key.read_bytes()) == 32
    assert re.fullmatch(r"S-[0-9A-HJKMNP-TV-Z]{26}", workspace.read_default_session(ws))
    assert "Programación" in str(ws.root) and " " in str(ws.root)


def test_init_is_idempotent(ws):
    names = ["config.json", "redaction.key", "default-session", ".gitignore"]
    before = {ws.path(n): ws.path(n).read_bytes() for n in names}
    _, created = workspace.init_workspace(ws.root)
    assert not created and all(p.read_bytes() == b for p, b in before.items())


def test_init_does_not_rewrite_existing_config(ws):
    ws.path("config.json").write_bytes(b"{}")
    _, created = workspace.init_workspace(ws.root)
    assert not created and ws.path("config.json").read_bytes() == b"{}"


def test_config_bytes_pretty_sorted(ws):
    assert ws.path("config.json").read_bytes() == workspace.config_bytes(workspace.default_config("My Skills"))


def test_default_config_values():
    cfg = workspace.default_config("x")
    assert cfg["defaults"] == {"timeout_seconds": 900, "max_log_mb": 50, "flaky_reruns": 3}
    assert cfg["project"]["trust"] == "untrusted"
    schema.validate(cfg, "config.schema.json")


def test_config_schema_rules():
    good = workspace.default_config("x")
    good["environments"].append({"id": "db", "class": "ephemeral", "ephemeral_basis": "user-declared",
                                 "dsn_env": "QA_DB_URL"})
    good["gate"]["overrides"] = {"c": {"accepted_in": "EVT-" + "0" * 26, "params": {"any": 1}}}
    schema.validate(good, "config.schema.json")

    def bad(mutate):
        cfg = workspace.default_config("x")
        mutate(cfg)
        with pytest.raises(QaError) as e:
            schema.validate(cfg, "config.schema.json")
        assert e.value.code == "schema_invalid"

    bad(lambda c: c.update(extra=1))
    bad(lambda c: c["environments"][0].update(**{"class": "prod"}))
    bad(lambda c: c["environments"][0].update(dsn_env="postgres://u:p@h/db"))
    bad(lambda c: c["redaction"].update(extra_env_names=["not a name"]))
    bad(lambda c: c["standing_authorizations"].append({"environment": "local", "activities": ["nope"]}))
    bad(lambda c: c["gate"]["overrides"].update(x={"enabled": False}))
    bad(lambda c: c["gate"]["overrides"].update(x={"accepted_in": "EVT-1", "bogus": 1}))
    bad(lambda c: c["environments"][0].update(ephemeral_basis="other"))
    bad(lambda c: c["project"].update(trust="maybe"))
    bad(lambda c: c.update(schema_version="v1"))
    bad(lambda c: c.pop("redaction"))


def test_find_workspace_walks_up(ws):
    sub = ws.root / "a" / "b"
    sub.mkdir(parents=True)
    assert workspace.find_workspace(sub).root == ws.root


def test_find_workspace_missing(tmp_path):
    with pytest.raises(QaError) as e:
        workspace.find_workspace(tmp_path)
    assert e.value.code == "no_workspace"


def test_find_workspace_explicit(ws, tmp_path):
    sub = ws.root / "a"
    sub.mkdir()
    assert workspace.find_workspace(tmp_path, explicit=ws.root).root == ws.root
    with pytest.raises(QaError) as e:  # explicit means no walking up
        workspace.find_workspace(ws.root, explicit=sub)
    assert e.value.code == "no_workspace"


def test_schema_too_new_refuses_write(ws):
    cfg = workspace.default_config("x")
    cfg["schema_version"] = "2"
    ws.path("config.json").write_bytes(workspace.config_bytes(cfg))
    with pytest.raises(QaError) as e:
        workspace.check_schema_version(ws, for_write=True)
    assert e.value.code == "schema_too_new"
    assert workspace.check_schema_version(ws, for_write=False) == "schema_newer"


def test_schema_current_or_older_is_fine(ws):
    assert workspace.check_schema_version(ws, for_write=True) is None
    cfg = workspace.default_config("x")
    cfg["schema_version"] = "1.7"
    ws.path("config.json").write_bytes(workspace.config_bytes(cfg))
    assert workspace.check_schema_version(ws, for_write=False) is None


def test_default_session_invalid(ws):
    ws.path("default-session").write_text("../../etc/passwd\n")
    with pytest.raises(QaError) as e:
        workspace.read_default_session(ws)
    assert e.value.code == "invalid_session"


def test_write_atomic_replaces_whole_file(tmp_path):
    p = tmp_path / "f.json"
    p.write_bytes(b"old")
    workspace.write_atomic(p, b"new")
    assert p.read_bytes() == b"new" and [x.name for x in tmp_path.iterdir()] == ["f.json"]


def test_lock_reentrant_and_exclusive(ws, py):
    code = ("import sys; from pathlib import Path; from qa.workspace import find_workspace; from qa.errors import QaError\n"
            "try:\n    with find_workspace(Path(sys.argv[1])).lock(timeout_s=0.2): print('got')\n"
            "except QaError as e: print(e.code)")
    with ws.lock():
        with ws.lock():
            assert py.run(code, str(ws.root)).stdout.strip() == "lock_timeout"
        assert py.run(code, str(ws.root)).stdout.strip() == "lock_timeout"
    assert py.run(code, str(ws.root)).stdout.strip() == "got"


def test_lock_reentrant_across_equal_instances(ws):
    other = workspace.find_workspace(ws.root)
    assert other == ws
    with ws.lock(timeout_s=0.2):
        with other.lock(timeout_s=0.2):
            pass


def test_lock_excludes_other_thread(ws):
    result = []

    def worker():
        try:
            with ws.lock(timeout_s=0.2):
                result.append("got")
        except QaError as exc:
            result.append(exc.code)

    with ws.lock():
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert result == ["lock_timeout"]
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result == ["lock_timeout", "got"]
