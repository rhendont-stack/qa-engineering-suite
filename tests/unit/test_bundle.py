from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from qa import authz, bundle, compare, config, gate, ids, ledger, safetar, state
from qa.canonical import dumps, sha256_bytes
from qa.errors import QaError
from qa.workspace import init_workspace
from test_compare import _execution, _result

COMMIT = "a" * 40
OTHER = "b" * 40
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
STDOUT = b"collected 2 items\n"


def _record(ws, actor, letter, *, commit=COMMIT, argv=("pytest", "-q"), tests=(("t1", "passed"), ("t2", "passed")),
            status="passed", evidence=False, number=0):
    """One execution and its results in ``ws``'s ledger; with ``evidence`` its manifest and a log are on disk too."""
    exe_id = "EXE-" + "0" * 25 + letter
    data = _execution(exe_id)
    data["command"]["argv"] = list(argv)
    data["status"] = status
    data["git"] = {"commit": commit, "branch": "main", "dirty": False, "diff_hash": "sha256:" + "1" * 64}
    if evidence:
        folder = ws.path("evidence", exe_id)
        folder.mkdir(parents=True)
        (folder / "stdout.log").write_bytes(STDOUT)
        manifest = dumps({"stdout.log": {"sha256": sha256_bytes(STDOUT)}})
        (folder / "manifest.json").write_bytes(manifest)
        data["evidence_manifest"] = {"sha256": sha256_bytes(manifest), "files": ["stdout.log"], "ids": {}}
    ledger.append(ws, actor, "execution.recorded", data)
    for index, (test_id, outcome) in enumerate(tests):
        ledger.append(ws, actor, "result.recorded", _result(number + index + 1, exe_id, test_id, outcome))
    return exe_id


@pytest.fixture
def source(tmp_path, actor):
    ws = init_workspace(tmp_path / "ci")[0]
    _record(ws, actor, "C", evidence=True)
    return ws


def _bundle(ws, actor, tmp_path, name="out.tar.gz"):
    return bundle.create(ws, actor, tmp_path / name)


def _members(path):
    with open(str(path), "rb") as handle:
        return dict(safetar.read_members(handle, 1 << 28))


def _write(path, members):
    with tarfile.open(str(path), "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _files(ws):
    return {str(p.relative_to(ws.qa_dir)): p.read_bytes() for p in sorted(ws.qa_dir.rglob("*")) if p.is_file() and p.name != "lock"}


def test_bundle_round_trip_verifies_hashes(ws, source, actor, tmp_path):
    made = _bundle(source, actor, tmp_path)
    assert made["files"] >= 3 and made["state_hash"] == state.state_hash(state.load(source))
    done = bundle.ingest(ws, actor, tmp_path / "out.tar.gz")
    assert done["id"] == "BUNDLE-001" and done["verified_files"] == made["files"]
    folder = ws.path("imports", "BUNDLE-001")
    assert (folder / "evidence" / ("EXE-" + "0" * 25 + "C") / "stdout.log").read_bytes() == STDOUT
    st = state.load(ws)
    assert st["executions"] == {} and st["violations"] == []  # an imported run is not a local one
    imported = st["bundles"]["BUNDLE-001"]["executions"]["EXE-" + "0" * 25 + "C"]
    assert imported["status"] == "passed" and imported["commit"] == COMMIT and sorted(imported["tests"]) == ["t1", "t2"]


def test_bundle_bytes_are_stable_for_the_same_state(source, actor, tmp_path):
    first, second = _bundle(source, actor, tmp_path, "a.tar.gz"), _bundle(source, actor, tmp_path, "b.tar.gz")
    assert first["sha256"] == second["sha256"]


def _unportable_backslash(ws):
    (ws.path("evidence", "EXE-" + "0" * 25 + "C") / "bad\\name.log").write_bytes(b"x")


def _unportable_off_layout(ws):
    ws.path("evidence", "not-an-execution").mkdir()
    ws.path("evidence", "not-an-execution", "x.log").write_bytes(b"x")


@pytest.mark.parametrize("make", [_unportable_backslash, _unportable_off_layout])
def test_create_refuses_instead_of_silently_dropping_an_unportable_name(source, actor, tmp_path, make):
    make(source)
    with pytest.raises(QaError) as caught:
        _bundle(source, actor, tmp_path)
    assert caught.value.code == "bundle_unportable" and not (tmp_path / "out.tar.gz").exists()


def test_create_refuses_an_existing_output_and_one_inside_qa(source, actor, tmp_path):
    _bundle(source, actor, tmp_path)
    with pytest.raises(QaError) as caught:
        _bundle(source, actor, tmp_path)
    assert caught.value.code == "output_exists"
    with pytest.raises(QaError) as caught:
        bundle.create(source, actor, source.path("work", "x.tar.gz"))
    assert caught.value.code == "invalid_output"


def _flip(members):
    members["evidence/EXE-" + "0" * 25 + "C/stdout.log"] = b"edited after the fact\n"


def _edit_ledger(members):
    name = next(n for n in members if n.startswith("ledger/"))
    members[name] = members[name].replace(b"pytest", b"pytesx")


def _extra(members):
    members["evidence/EXE-" + "0" * 25 + "C/extra.log"] = b"not in the manifest"


def _drop(members):
    del members["evidence/EXE-" + "0" * 25 + "C/stdout.log"]


def _bad_manifest(members):
    members["manifest.json"] = b"not json"


def _outside_layout(members):
    members["scripts/run.sh"] = b"echo hi"
    manifest = json.loads(members["manifest.json"])
    manifest["files"]["scripts/run.sh"] = sha256_bytes(b"echo hi")
    members["manifest.json"] = dumps(manifest)


def _foreign_ledger_name(members):
    name = next(n for n in members if n.startswith("ledger/"))
    members["ledger/notes.jsonl"] = members.pop(name)
    manifest = json.loads(members["manifest.json"])
    manifest["files"]["ledger/notes.jsonl"] = manifest["files"].pop(name)
    members["manifest.json"] = dumps(manifest)


def _wrong_state_hash(members):
    manifest = json.loads(members["manifest.json"])
    manifest["state_hash"] = "sha256:" + "9" * 64
    members["manifest.json"] = dumps(manifest)


def _unchained(members):
    name = next(n for n in members if n.startswith("ledger/"))
    lines = members[name].split(b"\n")
    members[name] = b"\n".join(lines[1:])
    manifest = json.loads(members["manifest.json"])
    manifest["files"][name] = sha256_bytes(members[name])
    members["manifest.json"] = dumps(manifest)


TAMPERS = {"evidence_edited": _flip, "ledger_edited": _edit_ledger, "unlisted_member": _extra, "listed_member_gone": _drop,
           "manifest_not_json": _bad_manifest, "outside_layout": _outside_layout, "foreign_ledger_name": _foreign_ledger_name,
           "state_hash_lies": _wrong_state_hash, "ledger_cut": _unchained}


@pytest.mark.parametrize("how", sorted(TAMPERS))
def test_tampered_bundle_is_refused_and_records_nothing(ws, source, actor, tmp_path, how):
    _bundle(source, actor, tmp_path)
    members = _members(tmp_path / "out.tar.gz")
    TAMPERS[how](members)
    _write(tmp_path / "bad.tar.gz", members)
    before = _files(ws)
    with pytest.raises(QaError) as caught:
        bundle.ingest(ws, actor, tmp_path / "bad.tar.gz")
    assert caught.value.code == "bundle_invalid"
    assert _files(ws) == before  # no event, no partial directory
    assert not ws.path("imports").exists() or list(ws.path("imports").iterdir()) == []


def test_a_member_that_is_a_link_is_refused_before_anything_is_written(ws, actor, tmp_path):
    with tarfile.open(str(tmp_path / "evil.tar.gz"), "w:gz") as tar:
        info = tarfile.TarInfo("ledger/x.jsonl")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
    before = _files(ws)
    with pytest.raises(QaError) as caught:
        bundle.ingest(ws, actor, tmp_path / "evil.tar.gz")
    assert caught.value.code == "archive_unsafe" and _files(ws) == before


def test_imported_ledger_grants_no_authority(ws, tmp_path, actor):
    src = init_workspace(tmp_path / "ci")[0]
    authz.grant(src, actor, environment_id="local", targets=[], activities=["db-readonly"], limits={}, approval="ok",
                expires_at=ids.now_utc() + timedelta(days=1))
    _record(src, actor, "C")
    _bundle(src, actor, tmp_path)
    bundle.ingest(ws, actor, tmp_path / "out.tar.gz")
    st, cfg = state.load(ws), config.load_config(ws)
    assert st["authorizations"] == {} and "BUNDLE-001" in st["bundles"]
    decision = authz.check(st, cfg, activity="db-readonly", environment={"id": "local", "class": "local"}, now=ids.now_utc())
    assert decision.allowed is False
    assert ws.path("imports", "BUNDLE-001", "ledger").is_dir() and not list(ws.path("ledger").glob("*"))[1:]


def _crit(**over):
    c = {"id": "suite_executed", "type": "suite_executed", "blocking": True, "min_strength": None, "min_independence": "I3",
         "params": {"scope": "targeted", "clean_tree": False, "require_passed": True}}
    c.update(over)
    return c


def _context(commit=COMMIT):
    return gate.GateContext(target_commit=commit, independence="I1", integrity_problems=[],
                            kernel={"status": "ok", "source_checkout": False}, now=NOW)


def _outcome(ws, **over):
    return gate.evaluate_criterion(_crit(**over), state.load(ws), _context())


def _import(ws, actor, tmp_path, **record):
    src = init_workspace(tmp_path / ("ci%d" % len(list(tmp_path.glob("ci*")))))[0]
    _record(src, actor, "C", **record)
    out = tmp_path / ("b%d.tar.gz" % len(list(tmp_path.glob("b*.tar.gz"))))
    bundle.create(src, actor, out)
    return bundle.ingest(ws, actor, out)


def test_i3_needs_a_matching_comparison(ws, actor, tmp_path):
    local = _record(ws, actor, "D")
    assert _outcome(ws)["outcome"] == "NO_EVIDENCE"  # nobody reproduced it yet
    assert _outcome(ws, min_independence=None)["outcome"] == "MET"
    done = _import(ws, actor, tmp_path, tests=(("t1", "passed"), ("t2", "failed")), status="failed")
    assert [c["matching"] for c in done["comparisons"]] == [False]
    assert _outcome(ws)["outcome"] == "NO_EVIDENCE"  # it ran, but did not agree
    done = _import(ws, actor, tmp_path)
    assert [(c["local"], c["matching"]) for c in done["comparisons"]] == [(local, True)]
    result = _outcome(ws)
    assert result["outcome"] == "MET" and result["evidence"] == [local]


def test_a_matching_run_at_another_commit_is_no_comparison(ws, actor, tmp_path):
    _record(ws, actor, "D")
    done = _import(ws, actor, tmp_path, commit=OTHER)
    assert done["comparisons"] == [] and _outcome(ws)["outcome"] == "NO_EVIDENCE"


def test_a_different_command_is_no_comparison(ws, actor, tmp_path):
    _record(ws, actor, "D")
    assert _import(ws, actor, tmp_path, argv=("pytest", "-x"))["comparisons"] == []


def test_a_forged_comparison_for_another_commit_does_not_count(ws, actor, tmp_path):
    local = _record(ws, actor, "D")
    _import(ws, actor, tmp_path)
    ledger.append(ws, actor, "bundle.compared", {"bundle": "BUNDLE-001", "local": local, "remote": "EXE-" + "0" * 25 + "C",
                                                 "commit": OTHER, "matching": True, "differences": {}})
    ws2 = _context(commit=OTHER)
    st = state.load(ws)
    assert gate.evaluate_criterion(_crit(), st, ws2)["outcome"] == "NO_EVIDENCE"


def test_comparison_naming_nothing_known_is_a_violation(ws, actor):
    ledger.append(ws, actor, "bundle.compared", {"bundle": "BUNDLE-009", "local": "EXE-" + "0" * 25 + "D",
                                                 "remote": "EXE-" + "0" * 25 + "C", "commit": COMMIT, "matching": True,
                                                 "differences": {}})
    assert [v["kind"] for v in state.load(ws)["violations"]] == ["unknown_bundle"]


def test_compare_resolves_a_bundle_execution(ws, actor, tmp_path):
    local = _record(ws, actor, "D", tests=(("t1", "passed"), ("t2", "passed")))
    _import(ws, actor, tmp_path, tests=(("t1", "passed"), ("t2", "failed"), ("t3", "passed")))
    st = state.load(ws)
    remote = "BUNDLE-001:EXE-" + "0" * 25 + "C"
    diff = compare.compare(st, local, remote)
    assert diff["comparable"] and diff["new_failures"] == ["t2"] and diff["appeared"] == ["t3"] and diff["head"] == remote
    assert compare.compare(st, remote, local)["fixed"] == ["t2"]


@pytest.mark.parametrize("name", ["BUNDLE-009:EXE-" + "0" * 25 + "C", "BUNDLE-001:EXE-" + "0" * 25 + "Z", "BUNDLE-001:", "BUNDLE-001:x"])
def test_compare_refuses_an_unknown_bundle_or_execution(ws, actor, tmp_path, name):
    local = _record(ws, actor, "D")
    _import(ws, actor, tmp_path)
    with pytest.raises(QaError) as caught:
        compare.compare(state.load(ws), local, name)
    assert caught.value.code in ("unknown_bundle", "unknown_execution")


def test_cli_round_trip(ws, source, qa_cli, tmp_path):
    code, made = qa_cli("report", "bundle", "--output", str(tmp_path / "cli.tar.gz"), workspace=source.root)
    assert code == 0 and made["files"] >= 3 and made["sha256"].startswith("sha256:")
    code, done = qa_cli("ingest", "bundle", str(tmp_path / "cli.tar.gz"))
    assert code == 0 and done["id"] == "BUNDLE-001"
    code, out = qa_cli("ingest", "bundle", str(tmp_path / "missing.tar.gz"))
    assert (code, out["error"]["code"]) == (2, "file_unreadable")
    code, out = qa_cli("ingest", "junit", str(tmp_path / "x.xml"))
    assert (code, out["error"]["code"]) == (2, "usage")
