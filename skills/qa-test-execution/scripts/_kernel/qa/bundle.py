"""Portable evidence bundles: export this workspace's evidence, import another environment's (§5.2, §6.4 P).

``create`` packs the ledger, the evidence files and the rendered reports into one tar.gz with a manifest of SHA-256
hashes. ``ingest`` reads such a file as data and trusts none of it. The archive is read member by member in memory
(``safetar``), every file must be listed in the manifest with its hash, and the ledger chains, the evidence manifests
and the state hash the bundle claims must all check out against the files, in a staging directory under ``.qa/imports/``
that is renamed into place only when everything passed. A refused bundle records nothing and leaves nothing behind.

What an accepted bundle is allowed to be: data. Its ledger is never merged into this one, its grants and approvals are
only counted, and its executions are never entries of the local state; the only effect they have on a verdict is the
``bundle.compared`` event this module records when an imported run and a local run of the same command at the same commit
are put side by side (``gate``'s I3 rule).
"""
from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import stat
import tarfile
from pathlib import Path
from typing import Dict, List, Optional

from . import KERNEL_VERSION, compare, ids, integrity, ledger, safetar, state
from .canonical import dumps, loads, sha256_bytes
from .errors import QaError
from .workspace import Workspace, read_limited, write_atomic

# QaError codes raised here: output_exists, invalid_output, file_unreadable, bundle_unportable, bundle_invalid, bundle_too_large;
# archive_unsafe comes from safetar; plus ledger.append's.

FORMAT = "qa-bundle/1"
MANIFEST = "manifest.json"
MAX_BYTES = 1 << 29  # of content, packed or unpacked
_LAYOUT = re.compile(r"ledger/S-[0-9A-HJKMNP-TV-Z]{26}\.jsonl|evidence/EXE-[0-9A-HJKMNP-TV-Z]{26}/.+|reports/RPT-[0-9]{3,}/[^/]+")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_PROBLEMS_SHOWN = 5


def _invalid(reason: str, **details: object) -> QaError:
    return QaError("bundle_invalid", reason, **details)


# --------------------------------------------------------------------------- creating

def _plain_files(folder: Path, prefix: str) -> Dict[str, Path]:
    """{archive name: path} for each regular file under ``folder``; links, devices and the rest are left out."""
    found: Dict[str, Path] = {}
    for where, directories, names in os.walk(str(folder)):
        directories.sort()
        for name in sorted(names):
            path = Path(where) / name
            if stat.S_ISREG(os.lstat(str(path)).st_mode):
                found[prefix + path.relative_to(folder).as_posix()] = path
    return found


def _collect(ws: Workspace) -> Dict[str, bytes]:
    paths = {"ledger/" + path.name: path for _session, path in ledger._session_files(ws)[0]}
    for kind in ("evidence", "reports"):
        paths.update(_plain_files(ws.path(kind), kind + "/"))
    files: Dict[str, bytes] = {}
    total = 0
    for name in sorted(paths):
        if _LAYOUT.fullmatch(name) is None or "\\" in name or "\0" in name:
            raise QaError("bundle_unportable", "a workspace file has a name a bundle cannot carry; "
                          "rename or remove it", path=name.replace("\0", "?"))
        try:
            data = read_limited(paths[name], MAX_BYTES)
        except OSError as exc:
            raise QaError("file_unreadable", "a workspace file cannot be read", path=name, error=str(exc))
        total += len(data)
        if total > MAX_BYTES:
            raise QaError("bundle_too_large", "the workspace holds more than a bundle may", limit=MAX_BYTES)
        files[name] = data
    return files


def _pack(members: Dict[str, bytes]) -> bytes:
    """A tar.gz whose bytes depend only on the members: sorted, no times, no owners, no gzip name."""
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name in [MANIFEST] + sorted(n for n in members if n != MANIFEST):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(members[name]), 0o644, 0
                tar.addfile(info, io.BytesIO(members[name]))
    return raw.getvalue()


def create(ws: Workspace, actor: ledger.Actor, output: Path) -> dict:
    """Write the bundle to ``output`` (which must not exist) and say what it holds."""
    output = Path(output)
    target = output.parent.resolve() / output.name
    if ws.qa_dir.resolve() in target.parents:
        raise QaError("invalid_output", "a bundle is not written inside .qa/, where the next one would carry it", path=str(output))
    if os.path.lexists(str(target)):
        raise QaError("output_exists", "the output file exists; a bundle never replaces a file", path=str(output))
    with ws.lock():
        st = state.fold(ledger.iter_all_events(ws))  # as the importer folds it, with no machine's key involved
        files = _collect(ws)
        manifest = {"format": FORMAT, "kernel_version": KERNEL_VERSION, "state_hash": state.state_hash(st),
                    "heads": ledger.heads(ws), "files": {name: sha256_bytes(data) for name, data in files.items()}}
        data = _pack(dict(files, **{MANIFEST: dumps(manifest) + b"\n"}))
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise QaError("output_exists", "the output file exists; a bundle never replaces a file", path=str(output))
    except OSError as exc:
        raise QaError("invalid_output", "the output file cannot be created", path=str(output), error=str(exc))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(str(target))
        finally:
            raise
    return {"path": str(output), "sha256": sha256_bytes(data), "files": len(files) + 1, "bytes": len(data),
            "state_hash": manifest["state_hash"], "executions": len(st["executions"])}


# --------------------------------------------------------------------------- importing

def _read_archive(path: Path) -> bytes:
    try:
        return read_limited(path, MAX_BYTES, follow=True)
    except OSError as exc:
        raise QaError("file_unreadable", "the file cannot be read", path=str(path), error=str(exc))


def _read_manifest(members: Dict[str, bytes]) -> dict:
    if MANIFEST not in members:
        raise _invalid("the bundle has no manifest")
    try:
        manifest = loads(members[MANIFEST])
    except QaError:
        raise _invalid("the manifest is not JSON")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    heads = manifest.get("heads") if isinstance(manifest, dict) else None
    if (not isinstance(files, dict) or not isinstance(heads, dict) or manifest.get("format") != FORMAT
            or not isinstance(manifest.get("kernel_version"), str) or not isinstance(manifest.get("state_hash"), str)
            or _SHA.fullmatch(manifest["state_hash"]) is None
            or not all(isinstance(v, str) and _SHA.fullmatch(v) for v in files.values())):
        raise _invalid("the manifest does not describe a %s bundle" % FORMAT)
    return manifest


def _check_files(members: Dict[str, bytes], manifest: dict) -> None:
    listed, present = set(manifest["files"]), set(members) - {MANIFEST}
    for name in sorted(listed ^ present)[:_PROBLEMS_SHOWN]:
        raise _invalid("a file is %s the manifest" % ("missing from the archive but listed in" if name in listed else "in the archive but not in"),
                       file=name)
    for name in sorted(present):
        if _LAYOUT.fullmatch(name) is None:
            raise _invalid("a file is outside the bundle's layout", file=name)
        if sha256_bytes(members[name]) != manifest["files"][name]:
            raise _invalid("a file does not match its hash in the manifest", file=name)


def _stage(stage: Path, members: Dict[str, bytes]) -> None:
    stage.mkdir(mode=0o700, parents=True)
    for name in sorted(members):
        target = stage / name
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(target, members[name])
        except OSError as exc:
            raise _invalid("a file cannot be stored", file=name, error=type(exc).__name__)


def _verify(stage: Path, ws: Workspace, manifest: dict) -> dict:
    """The imported ledger folded as data, after every check that it is what the manifest says."""
    pseudo = Workspace(root=ws.root, qa_dir=stage)
    found = ledger.verify_chains(pseudo, foreign=False) + integrity._event_problems(pseudo)
    imported = state.fold(ledger.iter_all_events(pseudo))
    found += integrity.verify_evidence(pseudo, imported)
    if found:
        raise _invalid("the bundle does not verify", problems=[{"kind": p.kind, "where": p.where} for p in found[:_PROBLEMS_SHOWN]],
                       count=len(found))
    if ledger.heads(pseudo) != manifest["heads"]:
        raise _invalid("the ledger heads differ from the manifest's")
    if state.state_hash(imported) != manifest["state_hash"]:
        raise _invalid("the state recomputed from the bundle's ledger differs from the manifest's state hash")
    return imported


def _summaries(imported: dict) -> Dict[str, dict]:
    return {exe_id: {"status": record["status"], "reason": record["reason"], "strength": record["strength"],
                     "exit_code": record["exit_code"], "argv": list(record["command"]["argv"]), "activity": record["activity"],
                     "scope": record["scope"], "commit": (record["git"] or {}).get("commit"),
                     "tests": compare.tests_of(imported, exe_id)}
            for exe_id, record in sorted(imported["executions"].items())}


_DIFFERENCE_LISTS = ("new_failures", "fixed", "appeared", "disappeared", "still_failing")


def _comparison(st: dict, bundle_id: str, remote_id: str, remote: dict, local_id: str) -> dict:
    """Whether the local run and the imported one agree on status and on every test."""
    local = st["executions"][local_id]
    diff = compare.compare(st, local_id, "%s:%s" % (bundle_id, remote_id))
    same = local["status"] == remote["status"] and local["exit_code"] == remote["exit_code"]
    if diff["comparable"]:
        differences: Dict[str, object] = {name: len(diff[name]) for name in _DIFFERENCE_LISTS}
        matching = same and not any(diff[name] for name in _DIFFERENCE_LISTS if name != "still_failing")
    else:
        differences = {"comparable": False, "reason": diff["comparable_reason"]}
        matching = same
    return {"local": local_id, "remote": remote_id, "commit": remote["commit"], "matching": matching, "differences": differences}


def _pairs(st: dict, bundle_id: str, executions: Dict[str, dict]) -> List[dict]:
    """Each imported run beside each local run of the same command, scope and activity at the same commit."""
    shown = dict(st, bundles={bundle_id: {"executions": executions, "comparisons": []}})
    pairs = []
    for remote_id, remote in executions.items():
        if remote["commit"] is None:
            continue
        for local_id in sorted(st["executions"]):
            local = st["executions"][local_id]
            if ((local["git"] or {}).get("commit") == remote["commit"] and list(local["command"]["argv"]) == remote["argv"]
                    and local["activity"] == remote["activity"] and local["scope"] == remote["scope"]):
                pairs.append(_comparison(shown, bundle_id, remote_id, remote, local_id))
    return pairs


def ingest(ws: Workspace, actor: ledger.Actor, path: Path) -> dict:
    """Verify a bundle and store it under ``.qa/imports/``, record it, and compare its runs with the local ones."""
    path = Path(path)
    raw = _read_archive(path)
    members = dict(safetar.read_members(io.BytesIO(raw), MAX_BYTES))  # all of it is read before a byte is written
    manifest = _read_manifest(members)
    _check_files(members, manifest)
    with ws.lock():
        bundle_id = ledger.next_human_id(ws, "BUNDLE")
        imports = ws.path("imports")
        final, stage = imports / bundle_id, imports / (".staging-" + ids.ulid())
        if os.path.lexists(str(final)):
            raise _invalid("an import directory already exists for the next bundle id", id=bundle_id)
        try:
            _stage(stage, members)
            imported = _verify(stage, ws, manifest)
            os.rename(str(stage), str(final))
        except BaseException:
            shutil.rmtree(str(stage), ignore_errors=True)
            raise
        try:
            executions = _summaries(imported)
            st = state.load(ws)
            event = ledger.append(ws, actor, "bundle.ingested", {
                "id": bundle_id, "sha256": sha256_bytes(raw), "manifest_sha256": sha256_bytes(members[MANIFEST]),
                "source": {"kernel_version": manifest["kernel_version"], "state_hash": manifest["state_hash"],
                           "heads": manifest["heads"]},
                "files": len(members), "violations": len(imported["violations"]),
                "ignored": {"authorizations": len(imported["authorizations"])}, "executions": executions})
        except BaseException:
            shutil.rmtree(str(final), ignore_errors=True)
            raise
        comparisons = _pairs(st, bundle_id, executions)
        for pair in comparisons:
            ledger.append(ws, actor, "bundle.compared", dict(pair, bundle=bundle_id))
    return {"id": bundle_id, "event": event["id"], "sha256": sha256_bytes(raw), "verified_files": len(members),
            "path": final.relative_to(ws.root).as_posix(), "executions": sorted(executions),
            "ignored": {"authorizations": len(imported["authorizations"])}, "violations": len(imported["violations"]),
            "comparisons": [{k: pair[k] for k in ("local", "remote", "commit", "matching")} for pair in comparisons]}
