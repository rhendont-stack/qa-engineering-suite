from __future__ import annotations

import json
import os
import subprocess

import pytest

from helpers import finishes
from qa import canonical, ledger
from qa.errors import QaError

INIT = {"kernel_version": "0.1.0-dev", "schema_version": "1", "config_hash": "sha256:" + "0" * 64}


def _file(ws, actor):
    return ws.path("ledger", actor.session + ".jsonl")


def test_append_chains(ws, actor):
    e1 = ledger.append(ws, actor, "workspace.initialized", INIT)
    e2 = ledger.append(ws, actor, "workspace.initialized", INIT)
    assert (e1["seq"], e1["prev"], e2["seq"], e2["prev"]) == (1, None, 2, e1["hash"])
    assert e1["hash"] == canonical.hash_obj({k: v for k, v in e1.items() if k != "hash"})


def test_unknown_event_type(ws, actor):
    with pytest.raises(QaError) as e:
        ledger.append(ws, actor, "made.up", {})
    assert e.value.code == "unknown_event_type"


@pytest.mark.parametrize("bad", ["../defs", "events/../defs", "workspace.initialized/", "Workspace.initialized", "x", ""])
def test_event_type_must_not_name_a_path(ws, actor, bad):
    with pytest.raises(QaError) as e:
        ledger.append(ws, actor, bad, INIT)
    assert e.value.code == "unknown_event_type"


def test_data_is_validated(ws, actor):
    with pytest.raises(QaError) as e:
        ledger.append(ws, actor, "workspace.initialized", {"x": 1})
    assert e.value.code == "schema_invalid"


def test_session_id_traversal_refused(ws):
    bad = ledger.Actor("verifier", None, "S-../../etc/x", "I1")
    with pytest.raises(QaError) as e:
        ledger.append(ws, bad, "workspace.initialized", INIT)
    assert e.value.code == "invalid_session"


def test_invalid_actor_role_is_refused_by_envelope_schema(ws):
    bad = ledger.Actor("wizard", None, "S-" + "0" * 26, "I1")
    with pytest.raises(QaError) as e:
        ledger.append(ws, bad, "workspace.initialized", INIT)
    assert e.value.code == "schema_invalid"
    assert not ws.path("ledger", bad.session + ".jsonl").exists()


def test_stored_line_is_canonical_and_actor_has_no_independence(ws, actor):
    e = ledger.append(ws, actor, "workspace.initialized", INIT)
    raw = _file(ws, actor).read_bytes()
    assert raw == canonical.dumps(e) + b"\n"
    assert e["actor"] == {"role": "verifier", "skill": "qa-test-execution", "session": actor.session}
    assert "redactions" not in e


def test_verify_detects_modified_byte(ws, actor):
    for _ in range(3):
        ledger.append(ws, actor, "workspace.initialized", INIT)
    f = _file(ws, actor)
    lines = f.read_text().splitlines(True)
    lines[1] = lines[1].replace('"1"', '"2"', 1)
    f.write_text("".join(lines))
    assert [p.kind for p in ledger.verify_chains(ws)] == ["modified"]


def test_verify_detects_deleted_line(ws, actor):
    for _ in range(3):
        ledger.append(ws, actor, "workspace.initialized", INIT)
    f = _file(ws, actor)
    lines = f.read_text().splitlines(True)
    f.write_text(lines[0] + lines[2])
    assert "unchained" in [p.kind for p in ledger.verify_chains(ws)]


def test_anchor_detects_truncation_and_missing_session(ws, actor):
    for _ in range(3):
        ledger.append(ws, actor, "workspace.initialized", INIT)
    anchors = ledger.heads(ws)
    f = _file(ws, actor)
    f.write_text(f.read_text().splitlines(True)[0])
    assert [p.kind for p in ledger.verify_chains(ws, anchors)] == ["unchained"]
    f.unlink()
    assert [p.kind for p in ledger.verify_chains(ws, anchors)] == ["missing"]


def test_anchor_with_different_hash_at_its_seq_is_modified(ws, actor):
    for _ in range(2):
        ledger.append(ws, actor, "workspace.initialized", INIT)
    anchors = {actor.session: {"seq": 2, "hash": "sha256:" + "1" * 64}}
    assert [p.kind for p in ledger.verify_chains(ws, anchors)] == ["modified"]
    assert ledger.verify_chains(ws, ledger.heads(ws)) == []


def test_partial_trailing_line_refused(ws, actor):
    ledger.append(ws, actor, "workspace.initialized", INIT)
    with open(_file(ws, actor), "ab") as fh:
        fh.write(b'{"v":1,"se')
    with pytest.raises(QaError) as e:
        ledger.append(ws, actor, "workspace.initialized", INIT)
    assert e.value.code == "ledger_partial_line"
    assert [p.kind for p in ledger.verify_chains(ws)] == ["invalid"]
    assert [e["seq"] for e in ledger.iter_session(ws, actor.session)] == [1]


def test_complete_invalid_last_line_is_corrupt(ws, actor):
    ledger.append(ws, actor, "workspace.initialized", INIT)
    with open(_file(ws, actor), "ab") as fh:
        fh.write(b'{"not":"an envelope"}\n')
    before = _file(ws, actor).read_bytes()
    with pytest.raises(QaError) as e:
        ledger.append(ws, actor, "workspace.initialized", INIT)
    assert e.value.code == "ledger_corrupt"
    assert _file(ws, actor).read_bytes() == before
    assert [p.kind for p in ledger.verify_chains(ws)] == ["invalid"]
    assert [e["seq"] for e in ledger.iter_session(ws, actor.session)] == [1]


def test_invalid_line_in_the_middle_is_skipped_and_chain_continues(ws, actor):
    for _ in range(2):
        ledger.append(ws, actor, "workspace.initialized", INIT)
    f = _file(ws, actor)
    lines = f.read_bytes().splitlines(True)
    f.write_bytes(lines[0] + b"garbage\n" + lines[1])
    assert [p.kind for p in ledger.verify_chains(ws)] == ["invalid"]
    assert [e["seq"] for e in ledger.iter_session(ws, actor.session)] == [1, 2]


def test_foreign_file_in_ledger_dir_is_invalid_and_ignored_by_reads(ws, actor):
    ledger.append(ws, actor, "workspace.initialized", INIT)
    ws.path("ledger", "notes.jsonl").write_text("{}\n")
    assert [p.kind for p in ledger.verify_chains(ws)] == ["invalid"]
    assert len(ledger.iter_all_events(ws)) == 1
    assert list(ledger.heads(ws)) == [actor.session]


def test_concurrent_appends_keep_chain(ws, actor, py):
    code = ("import sys; from pathlib import Path; from qa import ledger, workspace\n"
            "w = workspace.find_workspace(Path(sys.argv[1])); a = ledger.Actor('verifier', None, sys.argv[2], 'I1')\n"
            "for _ in range(25): ledger.append(w, a, 'workspace.initialized', "
            "{'kernel_version':'0.1.0-dev','schema_version':'1','config_hash':'sha256:' + '0'*64})")
    procs = [subprocess.Popen(py.argv(code, str(ws.root), actor.session), env=py.env) for _ in range(4)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert ledger.verify_chains(ws) == []
    assert [e["seq"] for e in ledger.iter_session(ws, actor.session)] == list(range(1, 101))


def test_iter_all_events_breaks_ties_by_session_then_seq(ws, frozen_now):
    a, b = (ledger.Actor("verifier", None, s, "I1") for s in ("S-" + "0" * 25 + "2", "S-" + "0" * 25 + "1"))
    ledger.append(ws, a, "workspace.initialized", INIT)
    ledger.append(ws, b, "workspace.initialized", INIT)
    assert [e["actor"]["session"] for e in ledger.iter_all_events(ws)] == [b.session, a.session]


def test_human_ids_grow_past_999(ws):
    assert ledger.next_human_id(ws, "FND") == "FND-001"
    assert (ledger.format_human_id("FND", 7), ledger.format_human_id("FND", 1000)) == ("FND-007", "FND-1000")


def test_next_human_id_follows_highest_in_ledger(ws, actor):
    lines = []
    for n, hid in enumerate(["FND-009", "FND-002", "AUTH-500"], 1):
        body = {"v": 1, "seq": n, "id": "EVT-" + "0" * 25 + str(n), "ts": "2026-11-07T00:00:00.000Z",
                "type": "finding.opened", "actor": {"role": "verifier", "skill": None, "session": actor.session},
                "data": {"id": hid}, "prev": lines[-1]["hash"] if lines else None}
        lines.append(dict(body, hash=canonical.hash_obj(body)))
    _file(ws, actor).write_bytes(b"".join(canonical.dumps(e) + b"\n" for e in lines))
    assert ledger.verify_chains(ws) == []
    assert ledger.next_human_id(ws, "FND") == "FND-010"


def test_secret_in_data_is_redacted(ws, actor, monkeypatch):
    monkeypatch.setenv("CI_DEPLOY_TOKEN", "tok-" + "9f8e7d6c5b")
    e = ledger.append(ws, actor, "workspace.initialized", dict(INIT, kernel_version="tok-" + "9f8e7d6c5b"))
    assert "9f8e7d6c5b" not in json.dumps(e) and e["redactions"] == 1
    assert "9f8e7d6c5b" not in _file(ws, actor).read_text()
    assert e["hash"] == canonical.hash_obj({k: v for k, v in e.items() if k != "hash"})
    assert ledger.verify_chains(ws) == []


def test_validate_session_id_accepts_a_real_id():
    assert ledger.validate_session_id("S-" + "0" * 26) == "S-" + "0" * 26
    with pytest.raises(QaError):
        ledger.validate_session_id("S-" + "0" * 26 + "\n")


# --------------------------------------------------------------------------- final review: I9

def _session_path(ws, actor):
    return ws.path("ledger", actor.session + ".jsonl")


def test_append_does_not_follow_a_dangling_symlink(ws, actor, tmp_path):
    target = tmp_path / "planted"
    _session_path(ws, actor).symlink_to(target)
    with pytest.raises(QaError) as e:
        ledger.append(ws, actor, "workspace.initialized", INIT)
    assert e.value.code == "ledger_corrupt" and not target.exists()


def test_a_symlinked_session_is_foreign_and_not_read(ws, actor, tmp_path):
    copy = tmp_path / "elsewhere.jsonl"
    copy.write_bytes(b"")
    _session_path(ws, actor).symlink_to(copy)
    files, foreign = ledger._session_files(ws)
    assert files == [] and foreign == [actor.session + ".jsonl"]
    with pytest.raises(QaError) as e:
        list(ledger.iter_session(ws, actor.session))
    assert e.value.code == "ledger_corrupt"


def test_a_ledger_over_the_cap_is_refused(ws, actor, monkeypatch):
    _session_path(ws, actor).write_bytes(b"x" * 100)
    monkeypatch.setattr(ledger, "MAX_LEDGER_BYTES", 10)
    with pytest.raises(QaError) as e:
        list(ledger.iter_session(ws, actor.session))
    assert e.value.code == "ledger_corrupt"


def test_a_fifo_session_is_refused_without_hanging(ws, actor):
    os.mkfifo(str(_session_path(ws, actor)))
    assert ledger._session_files(ws)[0] == []
    with pytest.raises(QaError) as e:
        finishes(lambda: list(ledger.iter_session(ws, actor.session)))
    assert e.value.code == "ledger_corrupt"
    with pytest.raises(QaError) as e:
        finishes(lambda: ledger.append(ws, actor, "workspace.initialized", INIT))
    assert e.value.code == "ledger_corrupt"
