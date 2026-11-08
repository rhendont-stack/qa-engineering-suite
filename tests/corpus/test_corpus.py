from __future__ import annotations

import getpass
import importlib.util
import json
import socket
from pathlib import Path

import pytest

from qa import ingest

CORPUS = Path(__file__).resolve().parent
ENTRIES = json.loads((CORPUS / "MANIFEST.json").read_text())


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["file"] for e in ENTRIES])
def test_corpus_matches_tool_console(entry):
    s = ingest.parse_report(entry["format"], CORPUS / entry["file"], 50 * 2**20)
    got = {"total": s.total, "passed": s.passed, "failed": s.failed, "errors": s.errors, "skipped": s.skipped} if s.kind == "tests" else {"alerts": len(s.alerts), "runs": s.runs}
    assert got == entry["expected"]


def corpus_files():
    return [p for p in CORPUS.rglob("*") if p.is_file() and "producers" not in p.relative_to(CORPUS).parts
            and p.name != "test_corpus.py" and "__pycache__" not in p.parts and p.relative_to(CORPUS).parts[0] != "guard"]


def sample_key():
    spec = importlib.util.spec_from_file_location("make_sample", CORPUS / "producers" / "gitleaks_sample" / "make_sample.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_key()


def test_corpus_files_are_the_manifest_files():
    listed = {e["file"] for e in ENTRIES}
    on_disk = {p.relative_to(CORPUS).as_posix() for p in corpus_files() if p.name != "MANIFEST.json"}
    assert listed == on_disk


def test_corpus_has_no_secret_and_no_trace_of_this_machine():
    key = sample_key()
    assert key.startswith("AKIA") and len(key) == 20
    forbidden = {"key": key, "key body": key[4:], "home directory": str(Path.home()), "user name": getpass.getuser(),
                 "hostname": socket.gethostname().split(".")[0]}
    files = corpus_files() + [CORPUS / "MANIFEST.json"]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for label, value in forbidden.items():
            assert value not in text, "%s found in %s" % (label, path.relative_to(CORPUS))
