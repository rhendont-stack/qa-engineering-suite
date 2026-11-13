from __future__ import annotations

import copy
import fnmatch
import re
import urllib.parse
from typing import Any, List, Optional

from . import KERNEL_VERSION, SCHEMA_VERSION, container, integrity, ledger, schema, state, vocab
from .canonical import loads, sha256_bytes
from .errors import QaError
from .workspace import Workspace, config_bytes, read_config, write_atomic

# QaError codes raised here: config_invalid, approval_required, use_gate_policy_accept, config_drift
# (set_value only; check_drift returns it), container_engine_unapproved, plus whatever ledger.append raises.

_SCHEMA = "config.schema.json"
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
DRIFT_REFUSAL = ("config.json differs from the approval this machine recorded: it was edited outside the CLI, or accepted on "
                 'another machine or before approvals were bound to a machine; run `qa config accept --approval "<quote>"`')


def current_hash(ws: Workspace) -> str:
    return sha256_bytes(read_config(ws))


def accepted_hash(st: dict) -> Optional[str]:
    return st["config"]["accepted_hash"]


def _schema_errors(cfg: Any) -> List[str]:
    if not isinstance(cfg, dict):
        return ["config.json must be a JSON object"]
    return schema.errors(cfg, schema.load_schema(_SCHEMA), base=_SCHEMA)


def _read_schema_valid(ws: Workspace) -> dict:
    """Parse config.json and check its structure; raise config_invalid otherwise."""
    try:
        cfg = loads(read_config(ws))
    except QaError as exc:
        if exc.code == "config_invalid":
            raise
        raise QaError("config_invalid", "config.json is not valid JSON", errors=[exc.message or exc.code])
    errs = _schema_errors(cfg)
    if errs:
        raise QaError("config_invalid", "config.json does not match its schema", errors=errs)
    return cfg


def load_config(ws: Workspace) -> dict:
    cfg = _read_schema_valid(ws)
    errs = semantic_errors(cfg)
    if errs:
        raise QaError("config_invalid", "config.json breaks a policy rule", errors=errs)
    return cfg


def _matches_protected(glob: str) -> bool:
    for entry in vocab.PROTECTED_PATHS:
        entry = entry.lower()
        if entry.endswith("/"):
            if (glob.startswith(entry) or fnmatch.fnmatchcase(entry[:-1], glob)
                    or fnmatch.fnmatchcase(entry + "x", glob)):
                return True
        elif fnmatch.fnmatchcase(entry, glob):
            return True
    return False


def _protected(segments: List[str]) -> bool:
    """Check the normalized scope as written and with ``**/`` collapsed to zero directories."""
    collapsed = [s for i, s in enumerate(segments) if not (s == "**" and i < len(segments) - 1)]
    return any(_matches_protected("/".join(segs).lower()) for segs in (segments, collapsed))


def _scope_errors(scope: str) -> List[str]:
    if scope.startswith(("/", "\\", "~")) or _DRIVE_RE.match(scope):
        return ["write scope %r must be project-relative" % scope]
    if ".." in re.split(r"[/\\]", scope):
        return ["write scope %r must not contain '..'" % scope]
    segments = [s for s in scope.replace("\\", "/").split("/") if s not in ("", ".")]
    if not segments:
        return ["write scope %r selects nothing" % scope]
    if _protected(segments):
        return ["write scope %r covers agent or IDE configuration" % scope]
    return []


def semantic_errors(cfg: dict) -> List[str]:
    """Value rules the closed structural schema leaves open. Expects a schema-valid config."""
    errs: List[str] = []
    classes = {}
    for env in cfg.get("environments", []):
        eid = env.get("id")
        if eid in classes:
            errs.append("duplicate environment id %r" % eid)
        classes.setdefault(eid, env.get("class"))
        if env.get("class") == "ephemeral":
            basis = env.get("ephemeral_basis")
            if basis is None:
                errs.append("environment %r: class ephemeral needs ephemeral_basis" % eid)
            elif basis == "suite-created":
                errs.append("environment %r: only the suite records the suite-created basis, never config" % eid)
    for auth in cfg.get("standing_authorizations", []):
        env_id, acts = auth.get("environment"), auth.get("activities", [])
        cls = classes.get(env_id)
        if cls not in ("local", "ephemeral", "ci"):
            errs.append("standing authorization for %r: environment is not a declared local, ephemeral or ci environment" % env_id)
        for act in acts:
            if act in vocab.NEVER_ACTIVITIES:
                errs.append("standing authorization for %r: %s is never performed by the suite" % (env_id, act))
            elif cls == "ci" and act not in vocab.RUNNER_CONFINED:
                errs.append("standing authorization for %r: %s is not confined to the CI runner" % (env_id, act))
    for scope in cfg.get("write_scopes", []):
        errs.extend(_scope_errors(scope))
    d = cfg.get("defaults", {})
    for name, lo, hi in (("timeout_seconds", 1, 86400), ("max_log_mb", 1, 1024), ("flaky_reruns", 0, 10)):
        v = d.get(name)
        if isinstance(v, int) and not isinstance(v, bool) and not lo <= v <= hi:
            errs.append("defaults.%s must be between %d and %d" % (name, lo, hi))
    gate = cfg.get("gate", {})
    if gate.get("profile") not in vocab.PROFILES:
        errs.append("gate.profile must be one of %s" % ", ".join(vocab.PROFILES))
    for name, entry in gate.get("overrides", {}).items():
        if not isinstance(entry, dict) or "accepted_in" not in entry:
            errs.append("gate.overrides.%s needs accepted_in" % name)
    if "container" in cfg:
        try:
            container.parse_engine(cfg["container"]["engine"])
        except QaError as exc:
            errs.append("container.engine: %s" % exc.message)
    return errs


def write_config(ws: Workspace, cfg: dict) -> str:
    errs = _schema_errors(cfg) or semantic_errors(cfg)
    if errs:
        raise QaError("config_invalid", "configuration refused", errors=errs)
    data = config_bytes(cfg)
    with ws.lock():
        write_atomic(ws.path("config.json"), data)
    return sha256_bytes(data)


def check_drift(ws: Workspace, st: dict, actor: ledger.Actor, activity: str) -> Optional[str]:
    if activity == "static-analysis":
        return None
    if current_hash(ws) == accepted_hash(st):
        return None
    _read_schema_valid(ws)  # redaction reads this file; an unusable one cannot be recorded
    with ws.lock():
        current = current_hash(ws)
        fresh = state.load(ws)
        if current == accepted_hash(fresh):
            return None
        if not any(d["hash"] == current for d in fresh["config"]["drift"]):
            ledger.append(ws, actor, "config.drift_detected",
                          {"hash": current, "accepted_hash": accepted_hash(fresh)})
    return "config_drift"


def _parse_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return loads(value)
    except QaError as exc:
        if exc.code != "invalid_json":
            raise
        return value


def set_value(ws: Workspace, actor: ledger.Actor, key: str, value: Any, approval: str) -> dict:
    if key == "gate" or key.startswith("gate."):
        raise QaError("use_gate_policy_accept", "gate settings change through `qa gate policy accept`", key=key)
    if key == "container":
        raise QaError("config_invalid", "the engine is set on its own, with its own record", errors=["use container.engine"])
    if not approval.strip():
        raise QaError("approval_required", "a configuration change needs the user's approval quote")
    new = _parse_value(value)
    with ws.lock():
        if current_hash(ws) != accepted_hash(state.load(ws)):
            raise QaError("config_drift", DRIFT_REFUSAL)
        cfg = load_config(ws)
        before = read_config(ws)
        if key == "container.engine":
            cfg.setdefault("container", {})  # optional, so a workspace made before this setting has no such object
        parts = key.split(".")
        node: Any = cfg
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if not all(parts) or not isinstance(node, dict):
            raise QaError("config_invalid", "key path must name members of existing objects",
                          errors=["unusable key %r" % key])
        old = copy.deepcopy(node.get(parts[-1]))
        node[parts[-1]] = new
        new_hash = write_config(ws, cfg)
        try:
            return ledger.append(ws, actor, "config.changed",
                                 {"key": key, "old": old, "new": new, "hash": new_hash, "approval": approval})
        except BaseException:
            write_atomic(ws.path("config.json"), before)
            raise


def accept(ws: Workspace, actor: ledger.Actor, approval: str) -> dict:
    if not approval.strip():
        raise QaError("approval_required", "accepting a configuration needs the user's approval quote")
    with ws.lock():
        load_config(ws)
        return ledger.append(ws, actor, "config.accepted", {"hash": current_hash(ws), "approval": approval})


def container_engine(ws: Workspace) -> str:
    """The engine this workspace may spawn. Only a workspace that never recorded one gets the default without a check.

    ``config.json`` may be tracked, so its text proves nothing: a value counts only as the last one recorded with an
    approval on this machine (``authority``), in a ledger that ``integrity.authority`` does not refuse and with the file
    still as accepted. An engine recorded elsewhere is therefore no record at all. Accepting an
    edited file does not count, in either direction: going back to the default moves project code to the local daemon,
    so it needs ``qa config set`` like any other change of engine.
    """
    engine = load_config(ws).get("container", {}).get("engine", container.DEFAULT_ENGINE)
    st = state.load(ws)
    recorded = [entry["container_engine"] for entry in st["config"]["history"] if "container_engine" in entry]
    if not recorded and engine == container.DEFAULT_ENGINE:
        return engine
    refused = integrity.authority(ws)["refused"]
    if refused or current_hash(ws) != accepted_hash(st) or not recorded or recorded[-1] != engine:
        raise QaError("container_engine_unapproved", "this engine was not chosen through `qa config set` with an approval "
                      "in a ledger that can back it", refused=refused)
    return engine


def record_initialized(ws: Workspace, actor: ledger.Actor) -> dict:
    return ledger.append(ws, actor, "workspace.initialized", {
        "kernel_version": KERNEL_VERSION, "schema_version": SCHEMA_VERSION, "config_hash": current_hash(ws)})


def _bare_host(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def resolve_host(cfg: dict, host: str) -> Optional[dict]:
    want = _bare_host(host)
    for env in cfg.get("environments", []):
        for url in env.get("base_urls", []):
            try:
                name = urllib.parse.urlsplit(url).hostname
            except ValueError:
                continue
            if name is not None and name.lower() == want:
                return env
    return None
