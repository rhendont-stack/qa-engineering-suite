"""The evidence-recording run pipeline: one command in, one recorded execution out.

``execute`` checks the request, then walks the stages (§8.3, §8.4, §9.5):

1-4. ``_gate``: guard, config drift, authorization and attempt rules. All of them run, so
     ``status.decide`` applies rule 1's order whatever the combination. A run stopped here has
     no evidence directory and is recorded by ``_record_blocked``.
5-9. ``_run_command``: create the evidence directory, snapshot the declared reports, run on the
     host or in a container (``_on_host``, ``_in_container``), redact and store the reports, remove
     anything undeclared, store the logs, decide the status, record the execution and its results.

A request with ``container`` never reaches the host: a failure of the engine or of a pipeline step
is a ``blocked`` run with a remedy. Only such a request is authorized as ``containerized``.

Everything stored or recorded goes through one redactor built from the run's own environment,
and is hashed after redaction.
"""
from __future__ import annotations

import dataclasses
import glob
import math
import os
import posixpath
import re
import shlex
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Set, Tuple, Union

from . import (KERNEL_VERSION, authz, config, container, envinfo, findings, gitinfo, guard, ids, ingest, integrity, ledger,
               redact, runner, schema, state, status, vocab, worktree)
from .canonical import dumps, loads, sha256_bytes, sha256_file
from .errors import QaError
from .workspace import FileTooLarge, Workspace, read_limited, write_atomic

EVIDENCE_TOKEN = "{evidence}"
_CONTAINER_EVIDENCE_BYTES = 256 * 2 ** 20  # one cap covers a stream and the copied-out tar; a normal evidence directory fits
_INSTALL_ACTIVITY = "install-dependencies"
_SERVICE_ACTIVITY = "test-execution-local"  # a service script runs project code, as the tests do
_INPUT_ROOT = [".qa", "work"]
_MAX_INPUTS = 64
_MAX_INPUT_BYTES = 16 * 2 ** 20
_SCOPES = ("targeted", "full")
_ATTEMPT_ACTIVITY = "test-execution-local"
_DEFAULT_ACTIVITY = "static-analysis"  # allowed without authority (§12.2)
_LOAD_TEST = "load-test"  # its grants carry max_vus, max_rps and max_duration_s (§12.2)
_ENV_VALUE_NAMES = ("CI", "NODE_ENV")
_STDERR_TAIL = 64 * 1024
_MIB = 2 ** 20
_CATEGORY = {"$ref": "defs.schema.json#/$defs/category"}
_UNDECLARED = {"id": "<undeclared>", "class": "production"}  # not "production": a real environment may hold that id
_MEDIA_TYPES = {"junit": "application/xml", "ctrf": "application/json", "sarif": "application/sarif+json", "lcov": "text/plain",
                "cobertura": "application/xml", "jacoco": "application/xml", "mutation": "application/json", "k6": "application/json"}
_FLAKY_KEYS = ("attempts", "passes", "ratio", "flaky", "status", "reproducible")
_Snapshot = Dict[str, Optional[Tuple[int, str]]]  # {real path: (st_mtime_ns, sha256)}
_BEFORE_INSTALL = ("volume_create", "copy_in", "control_in", None)  # where a failed pipeline stopped when no install had started; None: no step
_CONTAINER_REMEDIES = {
    "container_engine_unavailable": "start the container engine, or check the host set in container.engine",
    "container_image_missing": "pull the image once, by digest, on the engine; a run never pulls",
    "container_step_failed": "check the engine's disk and that the image can run tar and sh as root",
    "container_spec_invalid": "run from a project directory whose path holds only letters, digits, '.', '_' and '-'",
    "container_stdin_unreadable": "check that the temporary directory is writable",
    "container_cleanup_failed": "remove the objects named here from the engine by hand",
    "project_too_large": "keep large files out of the project with .gitignore",
    "project_unlistable": "repair the repository so that git can list its files",
    "project_unreadable": "make every project file readable",
}
_NO_REMEDY = "check that the temporary directory is writable and that the project can be read"


def _recordable(text: str) -> str:
    """Text the OS hands over can hold surrogate escapes (non-UTF-8 bytes), which the ledger cannot store (T14-R21)."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _encodable(text: str) -> bool:
    return text == _recordable(text)


@dataclass(frozen=True)
class RunRequest:
    command: Union[List[str], str]
    shell: bool = False
    cwd: Optional[Path] = None
    reports: Tuple[Tuple[str, str], ...] = ()
    timeout_s: Optional[float] = None
    attempt_of: Optional[str] = None
    activity: str = "test-execution-local"
    environment: str = "local"
    scope: str = "targeted"
    allow_empty: bool = False
    category: str = "functional"
    container: Optional[str] = None  # name@sha256:<digest>; set, the command runs only in a container from this image
    install: Optional[str] = None    # shell text run first in that container, with a network
    service: Optional[str] = None    # name@sha256:<digest> of a fixture service the tests share loopback with
    service_run: Optional[str] = None  # shell text the service runs
    service_user: Optional[str] = None  # UID or UID:GID the service runs as
    inputs: Tuple[str, ...] = ()     # files under .qa/work/, relative to the workspace root; they land at /work/.qa-input/


class _Input(NamedTuple):
    path: str  # as requested, relative to the workspace root
    name: str  # relative to .qa/work/: its place under .qa-input/
    data: bytes  # read once: what is hashed is what is packed
    sha256: str


@dataclass
class _Run:
    """What every stage of one ``execute`` call shares."""
    ws: Workspace
    actor: ledger.Actor
    req: RunRequest
    cfg: dict
    environment: dict
    cwd: str
    command: dict  # as recorded: it keeps the {evidence} token
    environ: Dict[str, str]
    redactor: redact.Redactor
    execution_id: str
    st: dict
    config_hash: str
    kernel: dict
    limitations: List[str] = field(default_factory=list)
    inputs: Tuple[_Input, ...] = ()

    @property
    def max_bytes(self) -> int:
        return self.cfg["defaults"]["max_log_mb"] * _MIB

    def note(self, text: str) -> None:
        text = _recordable(self.redactor.text(text)[0])  # a limitation can name a file; in the same order as _stored_name
        if text not in self.limitations:
            self.limitations.append(text)


@dataclass
class _Gate:
    """What stages 1-4 decided."""
    decision: guard.GuardDecision
    git: Optional[dict]
    policy_denied: bool = False
    config_drift: bool = False
    not_authorized: bool = False
    authorization: Optional[str] = None
    authorizations: List[dict] = field(default_factory=list)
    engine: Optional[container.Engine] = None

    @property
    def blocked(self) -> bool:
        return self.policy_denied or self.config_drift or self.not_authorized


class _Recorded(NamedTuple):
    verdict: status.StatusDecision
    evidence: Optional[str]
    summary: Optional[ingest.ReportSummary]
    limitations: List[str]
    measurements: Tuple[dict, ...] = ()


class _Ran(NamedTuple):
    """What one way of running the command hands to the shared stages."""
    outcome: runner.RunOutcome
    environment: dict  # the record's "environment", without "class"
    container: Optional[dict] = None  # the record's "container" block
    dependency_unavailable: bool = False
    anomalies: Tuple[str, ...] = ()
    service_log: Optional[Tuple[bytes, int]] = None  # (the log, bytes dropped) of a run that had a service


class _Reports(NamedTuple):
    entries: List[dict]  # the event's "reports" list, one per declared pair
    error: Optional[str]
    summary: Optional[ingest.ReportSummary]  # of the reports that decide a status; a measurement never joins it
    manifest: Dict[str, dict]
    measurements: Tuple[dict, ...] = ()


# --------------------------------------------------------------------------- the request

def _check_request(req: RunRequest) -> None:
    command = req.command
    well_formed = bool(command) if isinstance(command, str) else (
        isinstance(command, list) and bool(command) and all(isinstance(a, str) for a in command) and not req.shell)
    if not well_formed:
        raise QaError("invalid_command", "command must be a non-empty list of strings, or a non-empty shell string")
    if req.scope not in _SCOPES:
        raise QaError("invalid_scope", "scope must be targeted or full", scope=req.scope)
    if schema.errors(req.category, _CATEGORY):
        raise QaError("invalid_category", "unknown category", category=req.category)
    timeout = req.timeout_s
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not 0 < timeout < math.inf):
        raise QaError("invalid_timeout", "timeout_s must be a positive number of seconds", timeout_s=timeout)
    for fmt, _pattern in req.reports:
        if fmt not in ingest.FORMAT_KINDS:
            raise ingest.ReportInvalid("unknown_format", format=fmt)
    # A measurement report (coverage) goes beside either kind; test reports and alert reports do not go together.
    if len({ingest.FORMAT_KINDS[fmt] for fmt, _pattern in req.reports if not ingest.is_measurement(fmt)}) > 1:
        raise QaError("kind_mismatch", "one request cannot mix test reports and alert reports")
    argv = [command] if isinstance(command, str) else command
    if not all(map(_encodable, argv + [pattern for _fmt, pattern in req.reports] + [req.attempt_of or ""])):
        raise QaError("invalid_command", "a command that cannot be recorded must not run: it holds text UTF-8 cannot encode")
    _check_container_request(req)


def _check_container_request(req: RunRequest) -> None:
    wants_service = any(value is not None for value in (req.service, req.service_run, req.service_user))
    if req.container is None:
        if req.install is not None:
            raise QaError("install_needs_container", "--install runs only in a container: give --container too")
        if wants_service:
            raise QaError("service_needs_container", "a service runs only beside a container run: give --container too")
        if req.inputs:
            raise QaError("input_needs_container", "--input goes only into a container: give --container too")
        return
    if not container.is_pinned(req.container):
        raise QaError("container_image_unpinned", "the image must be pinned as name@sha256:<digest>", image=_recordable(str(req.container)))
    if req.install is not None and (not isinstance(req.install, str) or not req.install.strip() or not _encodable(req.install)):
        raise QaError("invalid_command", "the install script must be text that can be recorded")
    for _fmt, pattern in req.reports:  # only the evidence volume comes back, and nothing is read outside what came back
        if not pattern.startswith(EVIDENCE_TOKEN + "/") or ".." in pattern.split("/"):
            raise QaError("report_outside_evidence", "in a container a report must be under {evidence}/", pattern=pattern)
    if wants_service:
        _check_service_request(req)
    if not isinstance(req.inputs, (tuple, list)):
        raise QaError("invalid_command", "inputs must be a sequence of paths")


def _check_service_request(req: RunRequest) -> None:
    if not container.is_pinned(req.service):
        raise QaError("service_image_unpinned", "the service image must be pinned as name@sha256:<digest>",
                      image=None if req.service is None else _recordable(str(req.service)))
    if not isinstance(req.service_run, str) or not req.service_run.strip():
        raise QaError("service_run_required", "a service needs --service-run: the shell text that starts it")
    if not _encodable(req.service_run):
        raise QaError("invalid_command", "the service script must be text that can be recorded")
    if req.service_user is not None and not container.is_service_user(req.service_user):
        raise QaError("invalid_service_user", "the service user must be UID or UID:GID, in digits", user=_recordable(str(req.service_user)))


def _refused_input(index: int, why: str) -> QaError:
    return QaError("input_refused", "an input must be a regular file under .qa/work/, reached through real directories only, "
                   "named once; at most %d of them and %d bytes in all" % (_MAX_INPUTS, _MAX_INPUT_BYTES), index=index, why=why)


def _read_inputs(ws: Workspace, req: RunRequest) -> Tuple[_Input, ...]:
    """Each input, checked and read once, before anything runs or is recorded.

    The path is read as written, relative to the workspace root: ``.qa/work/`` spelled exactly, with no empty, ``.`` or
    ``..`` part and no NUL, and each path at most once. Then each component in turn is checked with ``lstat`` (a link
    anywhere is refused; directories above, a regular file last), and the file is opened once with ``O_NOFOLLOW`` and read
    from that descriptor: what is hashed is what is packed. The walk and the open are separate lookups, so the check
    holds against what was on disk, not against a process on this host rewriting ``.qa/work/`` at the same moment; such a
    writer already owns the workspace and is out of scope."""
    if len(req.inputs) > _MAX_INPUTS:
        raise _refused_input(_MAX_INPUTS, "too_many")
    found: List[_Input] = []
    seen: Set[str] = set()
    total = 0
    for index, text in enumerate(req.inputs):
        parts = text.split("/") if isinstance(text, str) and _encodable(text) and "\x00" not in text else []
        if len(parts) <= len(_INPUT_ROOT) or parts[:len(_INPUT_ROOT)] != _INPUT_ROOT or any(part in ("", ".", "..") for part in parts):
            raise _refused_input(index, "not_under_qa_work")
        if text in seen:  # two reads of one file could differ, and only the last would be packed
            raise _refused_input(index, "duplicate")
        seen.add(text)
        here = str(ws.root)
        for depth, part in enumerate(parts, 1):
            here = os.path.join(here, part)
            try:
                mode = os.lstat(here).st_mode
            except OSError:
                raise _refused_input(index, "not_a_regular_file") from None
            if stat.S_ISLNK(mode):
                raise _refused_input(index, "symlink")
            if not (stat.S_ISREG(mode) if depth == len(parts) else stat.S_ISDIR(mode)):
                raise _refused_input(index, "not_a_regular_file")
        try:
            data = read_limited(Path(here), _MAX_INPUT_BYTES - total)
        except FileTooLarge:
            raise _refused_input(index, "too_large") from None
        except OSError:  # replaced since the lstat: a link (ELOOP) or something that is not a file
            raise _refused_input(index, "not_a_regular_file") from None
        total += len(data)
        found.append(_Input(text, "/".join(parts[len(_INPUT_ROOT):]), data, sha256_bytes(data)))
    return tuple(found)


def _resolve_cwd(ws: Workspace, cwd: Optional[Path]) -> Tuple[str, str]:
    """(the real directory, the path recorded for it relative to the workspace root)."""
    root = os.path.realpath(str(ws.root))
    try:
        where = root if cwd is None else os.path.realpath(os.path.join(root, str(cwd)))
        inside = _encodable(str(cwd)) and os.path.isdir(where) and os.path.commonpath([root, where]) == root
    except ValueError:  # a NUL in the path
        inside = False
    if not inside:
        raise QaError("invalid_cwd", "cwd must be an existing directory inside the workspace", cwd=_recordable(str(cwd)))
    return where, _recordable(Path(os.path.relpath(where, root)).as_posix())  # a symlink can lead to any name


def execute(ws: Workspace, actor: ledger.Actor, req: RunRequest, *, environ: Optional[Mapping[str, str]] = None,
            grace_s: float = 5.0) -> dict:
    """Decide about ``req``, run it if it may run, and record what happened."""
    _check_request(req)
    inputs = _read_inputs(ws, req)
    cfg = config.load_config(ws)
    environment = next((e for e in cfg["environments"] if e["id"] == req.environment), None)
    if environment is None:
        raise QaError("unknown_environment", "config.json declares no such environment", environment=req.environment)
    cwd, recorded_cwd = _resolve_cwd(ws, req.cwd)
    run_environ = dict(os.environ if environ is None else environ)
    argv = [req.command] if isinstance(req.command, str) else list(req.command)
    kernel = integrity.kernel_integrity()
    redactor = redact.load_redactor(ws, environ=run_environ)
    recorded_argv = guard.mask_secret_flags(argv, lambda value: redactor.mask("secret_flag", value))
    run = _Run(ws=ws, actor=actor, req=req, cfg=cfg, environment=environment, cwd=cwd,
               command={"argv": recorded_argv, "shell": req.shell, "cwd": recorded_cwd}, environ=run_environ,
               redactor=redactor, execution_id=ids.new_id("EXE"),
               st=state.load(ws), config_hash=config.current_hash(ws),
               kernel={"version": KERNEL_VERSION, "manifest": kernel["manifest_sha256"]}, inputs=inputs)
    if kernel["status"] == "modified":
        run.note("kernel_modified")
    gate = _gate(run)
    recorded = _record_blocked(run, gate) if gate.blocked else _run_command(run, gate, grace_s)
    raised = {"opened": 0, "updated": 0}
    if recorded.summary is not None and recorded.summary.kind == "alerts":  # a timed-out scan's partial alerts are real too
        raised = findings.ingest_alerts(ws, actor, _redacted(run.redactor, recorded.summary), execution_id=run.execution_id,
                                        category=req.category)
    return {
        "execution": run.execution_id,
        "status": recorded.verdict.status,
        "reason": recorded.verdict.reason,
        "strength": recorded.verdict.strength,
        "anomalies": list(recorded.verdict.anomalies),
        "tests": _counts(recorded.summary),
        "flaky": flaky_view(state.load(ws), run.execution_id) if req.attempt_of is not None else {},
        "evidence": recorded.evidence,
        "findings": raised,
        "limitations": recorded.limitations,
        **({"measurements": list(recorded.measurements)} if recorded.measurements else {}),
    }


def _redacted(redactor: redact.Redactor, summary: ingest.ReportSummary) -> ingest.ReportSummary:
    """The alerts with every text field through a run's own redactor, as every other run event is (T14-R7)."""
    clean = tuple(dataclasses.replace(a, tool=redactor.text(a.tool)[0], rule_id=redactor.text(a.rule_id)[0],
                                      level=redactor.text(a.level)[0], message=redactor.text(a.message)[0],
                                      path=None if a.path is None else redactor.text(a.path)[0]) for a in summary.alerts)
    return dataclasses.replace(summary, alerts=clean)


def _counts(summary: Optional[ingest.ReportSummary]) -> Dict[str, int]:
    if summary is None:
        return dict.fromkeys(("total", "passed", "failed", "errors", "skipped"), 0)
    return {"total": summary.total, "passed": summary.passed, "failed": summary.failed,
            "errors": summary.errors, "skipped": summary.skipped}


# --------------------------------------------------------------------------- stages 1-4

def _gate(run: _Run) -> _Gate:
    req = run.req
    tree = worktree.containing(run.ws, run.st, run.cwd)  # a run inside a suite worktree is a run at that worktree's commit
    if tree is not None and req.container is not None:
        # The container receives an archive of the workspace root, not of the worktree, so recording the worktree's commit
        # would name a commit that was not tested. Until the archive can hold the worktree, refuse.
        raise QaError("container_in_worktree", "a container run cannot start inside a suite worktree: its archive would hold "
                      "the main working tree, not the worktree's commit", cwd=tree)
    decision = guard.classify(req.command, shell=req.shell, ws_root=run.ws.root, qa_dir=run.ws.qa_dir, config=run.cfg,
                              declared_activity=req.activity, cwd=req.cwd)
    if req.install is not None:
        decision = _with_script(run, decision, req.install, _INSTALL_ACTIVITY)
    if req.service_run is not None:
        decision = _with_script(run, decision, req.service_run, _SERVICE_ACTIVITY)
    git, git_limitation = gitinfo.git_info(run.ws.root if tree is None else run.ws.root / tree)
    if git_limitation is not None:
        run.note(git_limitation)
    elif tree is not None:
        git = dict(git, worktree=tree)
    gate = _Gate(decision, git)
    if decision.decision == "deny":
        gate.policy_denied = True
        run.note("guard rule %s denied the command: %s" % (decision.rule_id, decision.reason))
    refused = integrity.authority(run.ws)["refused"]
    for limitation in refused:
        run.note(limitation)
    if refused and set(decision.required_activities) != {_DEFAULT_ACTIVITY}:
        gate.not_authorized = True  # a shipped or unverifiable ledger authorizes nothing but the default (C1, I2)
    else:
        _check_drift(run, gate)
        _authorize(run, gate)
    refusal = _attempt_refusal(run, gate) if req.attempt_of is not None else None
    if refusal is not None:
        gate.policy_denied = True
        run.note("attempt_of refused: %s" % refusal)
    if req.container is not None:
        _choose_engine(run, gate)
    return gate


def _with_script(run: _Run, decision: guard.GuardDecision, script: str, activity: str) -> guard.GuardDecision:
    """``decision`` widened by a script that runs beside the command (the install step, or the service), which the guard
    reads as one more command under ``activity``: its activities and hosts join the run's, and a denial wins."""
    extra = guard.classify(script, shell=True, ws_root=run.ws.root, qa_dir=run.ws.qa_dir, config=run.cfg,
                           declared_activity=activity, cwd=run.req.cwd)
    if "deny" in (decision.decision, extra.decision):
        return decision if decision.decision == "deny" else extra
    return dataclasses.replace(
        decision, decision="require_grant" if "require_grant" in (decision.decision, extra.decision) else decision.decision,
        required_activities=tuple(dict.fromkeys(decision.required_activities + extra.required_activities)),
        hosts=tuple(dict.fromkeys(decision.hosts + extra.hosts)),
        environments=tuple(dict.fromkeys(decision.environments + extra.environments)))


def _choose_engine(run: _Run, gate: _Gate) -> None:
    """Which engine may receive the project is an authorization: ``config.container_engine`` alone decides it."""
    try:
        gate.engine = container.Engine(config.container_engine(run.ws))
    except QaError as exc:
        gate.not_authorized = True
        run.note("%s: nothing ran; remedy: choose the engine with `qa config set container.engine` and an approval" % exc.code)


def _check_drift(run: _Run, gate: _Gate) -> None:
    drifted = [a for a in gate.decision.required_activities
               if config.check_drift(run.ws, run.st, run.actor, a) is not None]
    if drifted:
        gate.config_drift = True
        run.note("config_drift: config.json differs from the configuration this machine accepted, needed for %s; "
                 'run `qa config accept --approval "<quote>"`' % ", ".join(drifted))


def _authorization_groups(cfg: dict, run_environment: dict, hosts: Tuple[str, ...],
                          named: Tuple[str, ...] = ()) -> List[Tuple[dict, List[str]]]:
    """The run's environment, each other environment a host or a named DSN resolves to, then the hosts nobody declared."""
    groups: Dict[str, Tuple[dict, List[str]]] = {run_environment["id"]: (run_environment, [])}
    for environment in cfg["environments"]:
        if environment["id"] in named:
            groups.setdefault(environment["id"], (environment, []))
    undeclared: List[str] = []
    for host in dict.fromkeys(hosts):
        environment = config.resolve_host(cfg, host)
        if environment is None:
            undeclared.append(host)
        else:
            groups.setdefault(environment["id"], (environment, []))[1].append(host)
    return list(groups.values()) + ([(_UNDECLARED, undeclared)] if undeclared else [])


def _authorize(run: _Run, gate: _Gate) -> None:
    """Every required activity against every group, each with that group's hosts only."""
    groups = _authorization_groups(run.cfg, run.environment, gate.decision.hosts, gate.decision.environments)
    now = ids.now_utc()
    # A grant never covers a per-target activity with no host, so checking one on an environment
    # no host resolves to would deny a run that the host's own environment authorizes.
    checks = [(activity, environment, hosts) for activity in gate.decision.required_activities
              for environment, hosts in groups
              if hosts or activity not in vocab.PER_TARGET_ACTIVITIES or len(groups) == 1]
    for position, (activity, environment, hosts) in enumerate(checks):
        verdict = authz.check(run.st, run.cfg, activity=activity, environment=environment, hosts=tuple(hosts), now=now,
                              containerized=run.req.container is not None)
        if position == 0:
            gate.authorization = verdict.authorization
        if verdict.allowed:
            entry = {"activity": activity, "environment": environment["id"], "authorization": verdict.authorization}
            if entry not in gate.authorizations:
                gate.authorizations.append(entry)
            continue
        gate.policy_denied = gate.policy_denied or verdict.reason == "policy_denied"
        gate.not_authorized = gate.not_authorized or verdict.reason == "not_authorized"
        detail = verdict.detail
        if detail == authz.UNTRUSTED and worktree.containing(run.ws, run.st, run.cwd) is not None:
            detail = authz.UNTRUSTED_IN_WORKTREE  # the container route the usual hint names is refused inside a worktree
        run.note("%s on environment %s: %s: %s" % (activity, environment["id"], verdict.reason, detail))


def _git_identity(git: Optional[dict]) -> Tuple[Optional[str], Optional[str]]:
    return (None, None) if git is None else (git["commit"], git["diff_hash"])


def _attempt_refusal(run: _Run, gate: _Gate) -> Optional[str]:
    """Why ``req.attempt_of`` may not be retried now, or None."""
    req, st = run.req, run.st
    parent = st["executions"].get(req.attempt_of)
    if parent is None:
        return "it names no recorded execution"
    if parent["status"] == "blocked":
        return "that execution was blocked"
    if req.activity != _ATTEMPT_ACTIVITY or parent["activity"] != _ATTEMPT_ACTIVITY:
        return "attempts are limited to %s" % _ATTEMPT_ACTIVITY
    if req.environment != parent["environment_id"]:
        return "that execution ran on environment %s" % parent["environment_id"]
    if _git_identity(gate.git) != _git_identity(parent["git"]):
        return "the commit or diff differs from that execution's"
    contained = parent.get("container", {})
    if (parent["isolation"], contained.get("image")) != (_isolation(req), req.container):
        return "that execution ran under another isolation or image"
    if (contained.get("service") or {}).get("image") != req.service:
        return "that execution ran with another service, or without one"
    root = state.chain_root(st, req.attempt_of)
    length = sum(1 for e in st["executions"].values() if e["status"] != "blocked" and state.chain_root(st, e["id"]) == root)
    if length >= 1 + run.cfg["defaults"]["flaky_reruns"]:
        return "the chain already has %d attempts" % length
    return None


def _append(run: _Run, event_type: str, data: dict) -> dict:
    """Redact with the run's own environment first: a secret that exists only there must not reach the ledger."""
    clean = run.redactor.obj(data)[0]
    ledger.append(run.ws, run.actor, event_type, clean)
    return clean


def _isolation(req: RunRequest) -> str:
    return "host" if req.container is None else "container"


def _execution_data(run: _Run, gate: _Gate, verdict: status.StatusDecision, facts: dict) -> dict:
    """The event's data: ``facts`` holds what differs between a run that started and one that did not."""
    req, actor = run.req, run.actor
    environ = {} if req.container is not None else run.environ  # none of the launcher's environment goes into a container
    return dict(
        facts,
        id=run.execution_id, command=run.command, git=gate.git, environment_id=req.environment,
        env_names=sorted(_recordable(name) for name in environ),
        env_values={name: _recordable(environ[name]) for name in _ENV_VALUE_NAMES if name in environ},
        isolation=_isolation(req), status=verdict.status, reason=verdict.reason, strength=verdict.strength,
        anomalies=list(verdict.anomalies), classification=verdict.classification,
        authorization=gate.authorization, authorizations=gate.authorizations, activity=req.activity, scope=req.scope,
        category=req.category, attempt_of=req.attempt_of, config_hash=run.config_hash, kernel=run.kernel,
        actor={"role": actor.role, "skill": actor.skill, "session": actor.session}, independence=actor.independence,
        limitations=run.limitations,
    )


def _record_blocked(run: _Run, gate: _Gate) -> _Recorded:
    req = run.req
    verdict = status.decide(status.StatusInputs(
        guard_denied=gate.policy_denied, not_authorized=gate.not_authorized, config_drift=gate.config_drift))
    now = ids.format_ts(ids.now_utc())
    record = _execution_data(run, gate, verdict, {
        "started_at": now, "ended_at": now, "duration_ms": 0, "exit_code": None, "signal": None, "timed_out": False,
        "environment": dict(envinfo.host_facts(), tools={}, **{"class": run.environment["class"]}),
        "evidence_manifest": None,
        "reports": [{"format": fmt, "pattern": pattern, "files": [], "error": None} for fmt, pattern in req.reports],
        "report": None, "redactions": 0,
    })
    with run.ws.lock():
        if gate.decision.decision == "deny":
            _append(run, "command.blocked", {"execution": run.execution_id, "activity": req.activity,
                                             "rule_id": gate.decision.rule_id, "reason": gate.decision.reason,
                                             "command": run.command})
        clean = _append(run, "execution.recorded", record)
    return _Recorded(verdict, None, None, clean["limitations"])


# --------------------------------------------------------------------------- the evidence directory

def _real_directory(path: Path) -> bool:
    """``lstat`` sees a symlink a tool planted instead of following it."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _unlink_unless_a_directory(path: Path) -> bool:
    """True when something other than a real directory stood at ``path`` and is now gone."""
    if _real_directory(path) or not os.path.lexists(path):
        return False
    os.unlink(path)
    return True


def _repair_evidence(run: _Run, directory: Path) -> None:
    """Whatever the tool did to the directory it was given, the run is still recorded (T14-R20)."""
    repairs = []
    try:
        replaced = _unlink_unless_a_directory(directory)
        if not os.path.lexists(directory):
            directory.mkdir(mode=0o700, parents=True)
            repairs.append("it was replaced" if replaced else "it was deleted")
        if stat.S_IMODE(os.lstat(directory).st_mode) != 0o700:
            os.chmod(directory, 0o700)
            repairs.append("its mode was changed")
        if _unlink_unless_a_directory(directory / "reports"):
            repairs.append("reports was not a directory")
    except OSError as exc:
        run.note("the evidence directory could not be repaired after the run (%s)" % type(exc).__name__)
    if repairs:
        run.note("the evidence directory was repaired after the run: %s" % ", ".join(repairs))


def _identities(paths: Iterable[Union[str, Path]]) -> Set[Tuple[int, int]]:
    """What the paths are on disk. A name would not do: some filesystems ignore case or normalization."""
    found = set()
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            continue
        found.add((info.st_dev, info.st_ino))
    return found


def _everything_under(root: str) -> List[str]:
    """Every entry below ``root``, each after what it holds. Symlinks are not entered, and depth is no limit."""
    found: List[str] = []
    folders = [root]
    while folders:
        try:
            with os.scandir(folders.pop()) as entries:
                for entry in entries:
                    found.append(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(entry.path)
        except OSError:  # unreadable: removing it then fails, and that is what gets reported
            continue
    return found[::-1]


def _clear_evidence(run: _Run, directory: Path, stored: Set[str], declared: Set[str]) -> None:
    """Keep only the stored copies: a file neither listed nor redacted must not sit in the store (§9.5, T14-R19)."""
    if not _real_directory(directory):  # a repair that failed: a planted symlink is never walked through
        return
    copies = [directory / name for name in stored]
    keep = _identities(copies + [copy.parent for copy in copies])
    raw_reports = _identities(declared)
    undeclared = 0
    for path in _everything_under(str(directory)):
        try:
            info = os.lstat(path)
            identity = (info.st_dev, info.st_ino)
            if identity in keep:
                continue
            is_file = not stat.S_ISDIR(info.st_mode)  # a symlink to a directory is unlinked, never entered
            (os.unlink if is_file else os.rmdir)(path)
        except OSError as exc:
            name = os.path.relpath(path, str(directory))
            run.note("could not remove %s from the evidence directory (%s)" % (name, type(exc).__name__))
            continue
        if is_file and identity not in raw_reports:
            undeclared += 1
    if undeclared:
        run.note("%d undeclared file(s) removed from the evidence directory; "
                 "declare a report pattern to keep a file" % undeclared)


def _store_file(directory: Path, name: str, data: bytes, media_type: str, redactions: int) -> dict:
    """Everything a run stores goes through here, so nothing is ever written through a planted symlink (T14-R20)."""
    target = directory / name
    if not _real_directory(directory) or os.path.islink(target.parent):
        raise NotADirectoryError(str(target.parent))
    target.parent.mkdir(mode=0o700, exist_ok=True)
    write_atomic(target, data)
    return {"sha256": sha256_bytes(data), "size": len(data), "media_type": media_type, "redactions": redactions}


def _store_evidence(run: _Run, directory: Path, manifest: Dict[str, dict], name: str, data: bytes, media_type: str,
                    redactions: int) -> None:
    try:
        manifest[name] = _store_file(directory, name, data, media_type, redactions)
    except OSError as exc:  # what the tool left there must not stop the run from being recorded (T14-R20)
        run.note("%s could not be stored (%s)" % (name, type(exc).__name__))


# --------------------------------------------------------------------------- report resolution

def _matches(pattern: str, directory: str, cwd: str) -> List[str]:
    """The regular, non-symlink files ``pattern`` names, as sorted real paths."""
    literal = os.path.join(cwd, pattern.replace(EVIDENCE_TOKEN, directory))
    if os.path.lexists(literal):
        candidates = [literal]
    else:  # both directories can hold glob characters: "My [x] Skills" is a legal path
        try:
            candidates = glob.glob(os.path.join(glob.escape(cwd), pattern.replace(EVIDENCE_TOKEN, glob.escape(directory))),
                                   recursive=True)
        except RecursionError:  # a tree deeper than the interpreter can walk (T14-R20)
            candidates = []
    found = set()
    for path in candidates:
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                found.add(os.path.realpath(path))
        except OSError:
            continue
    return sorted(found)


def _fingerprint(path: str) -> Optional[Tuple[int, str]]:
    try:
        return os.stat(path).st_mtime_ns, sha256_file(Path(path))
    except OSError:
        return None


def _snapshot(reports: Tuple[Tuple[str, str], ...], directory: str, cwd: str) -> _Snapshot:
    return {path: _fingerprint(path) for _fmt, pattern in reports for path in _matches(pattern, directory, cwd)}


def _produced(run: _Run, directory: str, snapshot: _Snapshot) -> List[List[str]]:
    """For each declared report, the files this run wrote or changed."""
    produced = []
    for _fmt, pattern in run.req.reports:
        matched = _matches(pattern, directory, run.cwd)
        produced.append([path for path in matched if path not in snapshot or snapshot[path] != _fingerprint(path)])
    return produced


def _name_key(name: str) -> str:
    """Names with one key may be one file: APFS ignores normalization and folds case in full, so ``ß`` is ``ss``."""
    return unicodedata.normalize("NFC", name).casefold()


def _free_name(name: str, taken: Set[str]) -> str:
    """``name``, or ``name`` with -2, -3, ... before its extension. ``taken`` holds ``_name_key`` values."""
    stem, extension = os.path.splitext(name)
    candidate, number = name, 1
    while _name_key(candidate) in taken:
        number += 1
        candidate = "%s-%d%s" % (stem, number, extension)
    taken.add(_name_key(candidate))
    return candidate


def _stored_name(redactor: redact.Redactor, path: str, taken: Set[str]) -> str:
    """A report's name in the store: without secrets, recordable (in that order, T14-R21), and not taken yet."""
    return _free_name(_recordable(redactor.text(os.path.basename(path))[0]), taken)


def _read_report(run: _Run, source: str) -> Optional[bytes]:
    name = os.path.basename(source)
    try:
        return read_limited(Path(source), run.max_bytes)
    except FileTooLarge:
        run.note("report_invalid: %s is over the defaults.max_log_mb cap and was not stored" % name)
        return None
    except OSError as exc:
        run.note("report_invalid: %s could not be read (%s)" % (name, type(exc).__name__))
        return None


def _copy_reports(run: _Run, directory: Path, produced: List[List[str]]) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """Store each produced file once, redacted. Returns {source: stored name} and the files' manifest entries."""
    formats: Dict[str, str] = {}
    for (fmt, _pattern), sources in zip(run.req.reports, produced):
        for source in sources:
            formats.setdefault(source, fmt)
    real_home = os.path.realpath(str(directory / "reports"))
    names: Dict[str, str] = {}
    manifest: Dict[str, dict] = {}
    taken: Set[str] = set()
    # A file the tool wrote straight into reports/ goes first, so it keeps its own name.
    for source in sorted(formats, key=lambda s: (os.path.dirname(s) != real_home, s)):
        data = _read_report(run, source)
        if data is None:
            continue
        name = _stored_name(run.redactor, source, taken)  # manifest keys stay clean
        clean, count = redacted_report(run.redactor, formats[source], data)
        try:
            manifest["reports/" + name] = _store_file(directory, "reports/" + name, clean, _MEDIA_TYPES[formats[source]], count)
        except OSError as exc:  # for example, a directory the tool left under that name
            run.note("report_invalid: %s could not be stored (%s)" % (name, type(exc).__name__))
            continue
        names[source] = name
    return names, manifest


def redacted_report(redactor: redact.Redactor, fmt: str, data: bytes) -> Tuple[bytes, int]:
    """A declared report's bytes as stored, and the number of redactions. A JSON report that parses is redacted value by
    value and serialised again, so a match cannot cut through one of its strings and leave a report that no longer parses
    (the byte-level pass did that to mutant source text holding a secret-like assignment); anything else is redacted as bytes."""
    if _MEDIA_TYPES[fmt].endswith("json"):
        done = redactor.json_document(data)
        if done is not None:
            return done
    return redactor.bytes_(data)


def _parse_copy(run: _Run, fmt: str, copy: Path) -> Optional[ingest.ReportSummary]:
    try:
        return ingest.parse_report(fmt, copy, run.max_bytes)
    except ingest.ReportInvalid as exc:
        run.note("report_invalid: reports/%s is not a valid %s report (%s)" % (copy.name, fmt, exc.details["detail"]))
        return None


def _store_reports(run: _Run, directory: Path, snapshot: _Snapshot) -> _Reports:
    produced = _produced(run, str(directory), snapshot)
    names, manifest = _copy_reports(run, directory, produced)
    _clear_evidence(run, directory, set(manifest), {source for sources in produced for source in sources})
    parsed: Dict[Tuple[str, str], Optional[ingest.ReportSummary]] = {}  # a file two patterns match counts once
    run_decides = any(not ingest.is_measurement(fmt) for fmt, _pattern in run.req.reports)
    entries = []
    for (fmt, pattern), sources in zip(run.req.reports, produced):
        files: List[str] = []
        error = None
        if not sources:
            error = "report_missing"
            run.note("report_missing: no file this run produced matches %s" % pattern)
        for source in sources:
            if source not in names:
                error = error or "report_invalid"
                continue
            files.append("reports/" + names[source])
            if (fmt, source) not in parsed:
                parsed[(fmt, source)] = _parse_copy(run, fmt, directory / "reports" / names[source])
            if parsed[(fmt, source)] is None:
                error = error or "report_invalid"
        entries.append({"format": fmt, "pattern": pattern, "files": files, "error": error})
        if error is not None and ingest.is_measurement(fmt) and run_decides:
            run.note("%s: %s" % ("measurement_" + error.split("_")[1], pattern))
    summaries = [s for s in parsed.values() if s is not None and s.kind not in ingest.MEASUREMENT_KINDS]
    measurements = tuple(_measurement(run, s, "reports/" + names[source], manifest)
                         for (_fmt, source), s in parsed.items() if s is not None and s.kind in ingest.MEASUREMENT_KINDS)
    # A measurement that is missing or invalid fails the run only when it is all the run declared; beside test or alert
    # reports it is noted (measurement_missing, measurement_invalid) and the verdict stays theirs.
    first_error = next((e["error"] for e in entries if e["error"] is not None
                        and (run_decides is False or not ingest.is_measurement(e["format"]))), None)
    return _Reports(entries, first_error, ingest.merge(summaries) if summaries else None, manifest, measurements)


def _flags(run_root: Path, summary: ingest.ReportSummary) -> List[str]:
    """The summary's anomalies, plus ``path_outside_project`` for an absolute path that is not under the workspace."""
    found = list(summary.anomalies)
    if any(ingest.outside_project(path, str(run_root)) for path in summary.paths):
        ingest.add_anomaly(found, "path_outside_project")
    return found


def _measurement(run: _Run, summary: ingest.ReportSummary, file: str, manifest: Dict[str, dict]) -> dict:
    entry = {"format": summary.format, "kind": summary.kind, "file": file, "sha256": manifest[file]["sha256"],
             "totals": summary.measurement_totals()}
    flags = _flags(run.ws.root, summary)
    return dict(entry, anomalies=flags) if flags else entry


def readable_measurements(record: dict) -> List[dict]:
    """The measurements a reader may show: those of a run that passed or failed. A blocked or inconclusive run
    parsed them (or not) under conditions nobody can vouch for, and a not_run run measured nothing."""
    return list(record.get("measurements", [])) if record.get("status") in ("passed", "failed") else []


def readable_ingested(record: dict) -> List[dict]:
    """The reports attached to a run with ``qa ingest``, read under the same rule as ``readable_measurements``."""
    return list(record.get("ingested", [])) if record.get("status") in ("passed", "failed") else []


def _report_block(summary: Optional[ingest.ReportSummary]) -> Optional[dict]:
    if summary is None:
        return None
    if summary.kind == "alerts":
        return {"format": summary.format, "kind": "alerts", "alerts": len(summary.alerts), "runs": summary.runs}
    block = {"format": summary.format, "kind": "tests", "tests": summary.total, "failures": summary.failed,
             "errors": summary.errors, "skipped": summary.skipped}
    if summary.metrics:  # what a load test measured: numbers beside the verdict, never part of it
        block.update(metrics={m.name: dict(m.values) for m in summary.metrics}, label="measured")
    return block


# What a load-test grant limits, and where a k6 summary says how far the run went: (limit, metric, statistic).
_LOAD_LIMITS = (("max_vus", "vus_max", "max"), ("max_rps", "http_reqs", "rate"))


def _load_limits_passed(run: _Run, gate: _Gate, summary: Optional[ingest.ReportSummary]) -> List[str]:
    """Each limit of the ``load-test`` grant that covered the run and that the run's summary shows it went past.

    Only a recorded grant carries limits; a run no grant covered has nothing to compare against."""
    if summary is None or (not summary.metrics and summary.run_duration_ms is None):
        return []
    measured = {m.name: m.values for m in summary.metrics}
    passed = []
    for entry in gate.authorizations:
        grant = run.st["authorizations"].get(entry["authorization"]) if entry["activity"] == _LOAD_TEST else None
        limits = grant.get("limits", {}).get(_LOAD_TEST, {}) if grant is not None else {}
        found = [(limit, "%s.%s" % (metric, stat), measured.get(metric, {}).get(stat)) for limit, metric, stat in _LOAD_LIMITS]
        found.append(("max_duration_s", "run_duration_ms / 1000",
                      None if summary.run_duration_ms is None else summary.run_duration_ms / 1000))
        passed += ["%s %s > %s %s of %s" % (where, value, limit, limits[limit], entry["authorization"])
                   for limit, where, value in found if isinstance(limits.get(limit), int) and value is not None and value > limits[limit]]
    return passed


# --------------------------------------------------------------------------- stages 5-9

def _quoted_for(directory: str, quote: Optional[str]) -> str:
    """``directory`` as one shell word where it stands: it can hold spaces ("My Skills"), and quotes do not nest."""
    if quote == "'":
        return directory.replace("'", "'\\''")
    if quote == '"':
        return re.sub(r'([\\"$`])', r"\\\1", directory)
    return shlex.quote(directory)


def _expanded_for_the_shell(command: str, directory: str) -> str:
    """``command`` with each ``{evidence}`` replaced, quoted for the quotes it sits inside."""
    out: List[str] = []
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        if command.startswith(EVIDENCE_TOKEN, index):
            out.append(_quoted_for(directory, quote))
            index += len(EVIDENCE_TOKEN)
            continue
        char = command[index]
        if char == "\\" and quote != "'":
            out.append(command[index:index + 2])
            index += 2
            continue
        if quote is None and char in "\"'":
            quote = char
        elif char == quote:
            quote = None
        out.append(char)
        index += 1
    return "".join(out)


def _concrete_command(req: RunRequest, directory: str) -> Union[List[str], str]:
    if req.shell:
        return _expanded_for_the_shell(req.command, directory)
    return [argument.replace(EVIDENCE_TOKEN, directory) for argument in req.command]


def _with_marker(data: bytes, dropped: int) -> bytes:
    return data + b"\n[TRUNCATED: %d bytes omitted by qa]\n" % dropped if dropped else data


def _write_evidence(run: _Run, directory: Path, streams: Dict[str, Tuple[bytes, int]], timeout_s: float,
                    report_entries: Dict[str, dict]) -> Tuple[dict, int]:
    """Write the logs, ``command.json`` and ``manifest.json``; returns (evidence_manifest, total redactions)."""
    req = run.req
    document, redactions = run.redactor.obj(dict(
        run.command, reports=[{"format": fmt, "pattern": pattern} for fmt, pattern in req.reports], timeout_s=timeout_s,
        activity=req.activity, environment=req.environment, scope=req.scope, category=req.category,
        allow_empty=req.allow_empty, attempt_of=req.attempt_of,
        **({} if req.container is None else {
            "container": req.container, "install": req.install, "service": req.service, "service_run": req.service_run,
            "service_user": req.service_user, "inputs": [{"path": item.path, "sha256": item.sha256} for item in run.inputs]})))
    manifest = dict(report_entries)
    _store_evidence(run, directory, manifest, "command.json", dumps(document) + b"\n", "application/json", redactions)
    for name, (data, count) in streams.items():
        _store_evidence(run, directory, manifest, name, data, "text/plain", count)
    manifest_bytes = dumps(manifest) + b"\n"
    _store_evidence(run, directory, {}, "manifest.json", manifest_bytes, "application/json", 0)  # not listed in itself
    evidence_manifest = {"sha256": sha256_bytes(manifest_bytes), "files": sorted(manifest),
                         "ids": {name: ids.ev_id(entry["sha256"]) for name, entry in manifest.items()}}
    return evidence_manifest, sum(entry["redactions"] for entry in manifest.values())


def _result_data(case: ingest.CaseResult, execution_id: str) -> dict:
    return {"id": ids.new_id("RES"), "execution": execution_id, "test_id": case.test_id, "status": case.status,
            "failure_kind": case.failure_kind, "skip_reason": case.skip_reason, "flaky": case.flaky,
            "duration_ms": case.duration_ms}


def _on_host(run: _Run, directory: Path, timeout_s: float, grace_s: float) -> _Ran:
    req = run.req
    fingerprint = envinfo.environment_fingerprint()
    outcome = runner.run_process(_concrete_command(req, str(directory)), shell=req.shell, cwd=Path(run.cwd), env=run.environ,
                                 timeout_s=timeout_s, max_bytes=run.max_bytes, grace_s=grace_s)
    _repair_evidence(run, directory)
    return _Ran(outcome, fingerprint)


def _container_script(req: RunRequest) -> str:
    command = _concrete_command(req, container.EVIDENCE_MOUNT)
    return command if isinstance(command, str) else shlex.join(command)


def _capped(data: bytes, dropped: int, limit: int) -> Tuple[bytes, int]:
    return data[:limit], dropped + max(len(data) - limit, 0)


def _without_host(engine: container.Engine, data: bytes) -> bytes:
    if engine.host is None:
        return data
    word = re.compile(br"(?<![A-Za-z0-9._-])%s(?![A-Za-z0-9._-])" % re.escape(engine.host.encode("ascii")))
    return word.sub(b"<engine-host>", data)


def _stream_for_the_record(run: _Run, engine: container.Engine, outcome: runner.RunOutcome) -> runner.RunOutcome:
    """The log cap the configuration sets, and no word of the host: ssh writes on the same stream as the container."""
    stdout, stdout_dropped = _capped(outcome.stdout, outcome.stdout_dropped, run.max_bytes)
    stderr, stderr_dropped = _capped(outcome.stderr, outcome.stderr_dropped, run.max_bytes)
    return dataclasses.replace(outcome, stdout=_without_host(engine, stdout), stderr=_without_host(engine, stderr),
                               stdout_dropped=stdout_dropped, stderr_dropped=stderr_dropped)


def _service_log_for_the_record(run: _Run, engine: container.Engine, finished: container.ContainerOutcome) -> Tuple[bytes, int]:
    """The service's log under the same rules as the test streams, with what the record should say about it."""
    data, dropped = _capped(finished.service_log, finished.service_log_dropped, run.max_bytes)
    if finished.service_log_error is not None:
        run.note("service_log_unavailable: %s" % finished.service_log_error)
    if dropped:
        run.note("service_log_truncated")
    service = finished.service or {}
    if service.get("image_id") is None:
        run.note("service_undescribed: the engine did not describe the service after the tests, so the run is not counted as isolated")
    elif not service.get("running_at_end"):
        code = service.get("exit_code")
        run.note("service_stopped: the service had ended (exit %s) when the tests ended" % ("unknown" if code is None else code))
    return _without_host(engine, data), dropped


def _unpack_evidence(run: _Run, directory: Path, files: Dict[str, bytes]) -> None:
    """What the container left in its evidence volume, where a host run's tool would have left it. Names passed ``safetar``."""
    for name, data in files.items():
        try:
            (directory / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(directory / name, data)
        except OSError as exc:
            run.note("%s could not be unpacked (%s)" % (_recordable(name), type(exc).__name__))


def _service_block(run: _Run, outcome: Optional[container.ContainerOutcome]) -> Optional[dict]:
    """What the daemon said about the service; a service that never started is the request with nothing observed."""
    if run.req.service is None:
        return None
    if outcome is not None and outcome.service is not None:
        return outcome.service
    return {"image": run.req.service, "image_id": None, "network_mode": None, "running_at_end": False, "exit_code": None}


def _container_block(run: _Run, engine: container.Engine, outcome: Optional[container.ContainerOutcome], install_ran: bool) -> dict:
    return {"image": run.req.container, "image_id": (outcome.image_id or None) if outcome else None, "engine_kind": engine.kind,
            "daemon_version": outcome.facts.version if outcome else None,
            "network_mode": (outcome.network_mode or None) if outcome else None, "install_ran": install_ran,
            "service": _service_block(run, outcome)}


def _staged_inputs(scratch: Path, inputs: Iterable[_Input]) -> List[Tuple[str, Path]]:
    """The bytes that were read and hashed, written where only this run reads them: the archive packs exactly those."""
    staged = []
    for number, item in enumerate(inputs):
        path = scratch / ("input-%d" % number)
        write_atomic(path, item.data)
        staged.append((item.name, path))
    return staged


def _note_cleanup(run: _Run, names: Iterable[str]) -> None:
    if names:
        run.note("container_cleanup_failed: left on the engine: %s; remedy: %s" % (", ".join(names), _CONTAINER_REMEDIES["container_cleanup_failed"]))


def _not_contained(run: _Run, engine: container.Engine, started_at: str, exc: Exception) -> _Ran:
    """The pipeline stopped short: a blocked run that says what to do. Nothing ran on the host and nothing will."""
    details = exc.details if isinstance(exc, QaError) else {}
    code = exc.code if isinstance(exc, QaError) else "container_io_error"
    step = details.get("step")
    what = ", ".join(str(part) for part in (details.get("reason"), step) if part is not None)
    run.note("%s%s: nothing ran on the host; remedy: %s" % (code, " (%s)" % what if what else "", _CONTAINER_REMEDIES.get(code, _NO_REMEDY)))
    _note_cleanup(run, details.get("cleanup_failed", ()))
    now = ids.format_ts(ids.now_utc())
    nothing = runner.RunOutcome(exit_code=None, signal=None, timed_out=False, spawn_error=None, started_at=started_at, ended_at=now,
                                duration_ms=0, stdout=b"", stderr=b"", stdout_dropped=0, stderr_dropped=0, orphaned_output=False)
    install_ran = run.req.install is not None and step not in _BEFORE_INSTALL
    return _Ran(nothing, dict(envinfo.host_facts(), tools={}), _container_block(run, engine, None, install_ran), dependency_unavailable=True)


def _in_container(run: _Run, gate: _Gate, directory: Path, timeout_s: float) -> _Ran:
    req, engine = run.req, gate.engine
    started_at = ids.format_ts(ids.now_utc())
    if gate.git is not None and gate.git["dirty"]:
        run.note("dirty_worktree: the archive holds the working tree as it is, not the commit; a tracked file that is gone is not in it")
    try:
        with tempfile.TemporaryDirectory(prefix="qa-archive-") as scratch:
            archive = Path(scratch) / "project.tar"
            container.project_archive(run.ws.root, archive, container.DEFAULT_ARCHIVE_MAX_BYTES,
                                      inputs=_staged_inputs(Path(scratch), run.inputs))
            service = None if req.service is None else container.ServiceSpec(image=req.service, script=req.service_run, user=req.service_user)
            finished = container.run(engine, container.ContainerSpec(
                execution_id=run.execution_id, image=req.container, script=_container_script(req), install_script=req.install,
                archive=archive, workdir=posixpath.normpath(posixpath.join(container.WORK_MOUNT, run.command["cwd"])), timeout_s=timeout_s,
                max_bytes=max(run.max_bytes, _CONTAINER_EVIDENCE_BYTES), service=service))
    except (QaError, OSError) as exc:
        return _not_contained(run, engine, started_at, exc)
    _note_cleanup(run, finished.cleanup_failed)
    outcome = _stream_for_the_record(run, engine, finished.run)
    environment = envinfo.container_facts(finished.facts.os, finished.facts.arch)
    block = _container_block(run, engine, finished, finished.install_ran)
    if finished.install_failed:
        how = "timed out" if outcome.timed_out else "exited %s" % outcome.exit_code
        run.note("the install step %s; the tests did not run and the logs are the install's; remedy: fix the install script or its grant" % how)
        return _Ran(dataclasses.replace(outcome, exit_code=None, signal=None, timed_out=False), environment, block, dependency_unavailable=True)
    if finished.evidence_error is not None:
        run.note("evidence_not_copied: %s" % finished.evidence_error)
    service_log = None if req.service is None else _service_log_for_the_record(run, engine, finished)
    _unpack_evidence(run, directory, finished.evidence)
    _repair_evidence(run, directory)
    return _Ran(outcome, environment, block, anomalies=finished.anomalies, service_log=service_log)


def _no_reports(req: RunRequest) -> _Reports:
    return _Reports([{"format": fmt, "pattern": pattern, "files": [], "error": None} for fmt, pattern in req.reports], None, None, {})


def _run_command(run: _Run, gate: _Gate, grace_s: float) -> _Recorded:
    req = run.req
    directory = run.ws.path("evidence", run.execution_id)
    directory.mkdir(mode=0o700, parents=True)
    timeout_s = req.timeout_s if req.timeout_s is not None else run.cfg["defaults"]["timeout_seconds"]
    snapshot = _snapshot(req.reports, str(directory), run.cwd)
    ran = _on_host(run, directory, timeout_s, grace_s) if req.container is None else _in_container(run, gate, directory, timeout_s)
    outcome = ran.outcome
    stdout, stdout_redactions = run.redactor.bytes_(outcome.stdout)
    stderr, stderr_redactions = run.redactor.bytes_(outcome.stderr)
    reports = _no_reports(req) if ran.dependency_unavailable else _store_reports(run, directory, snapshot)
    over = _load_limits_passed(run, gate, reports.summary)
    for text in over:
        run.note("load_limit_exceeded: " + text)
    anomalies = ran.anomalies + (("load_limit_exceeded",) if over else ())
    verdict = status.decide(status.StatusInputs(
        spawn_error=outcome.spawn_error, shell=req.shell or req.container is not None, exit_code=outcome.exit_code,
        timed_out=outcome.timed_out, dependency_unavailable=ran.dependency_unavailable,
        report_declared=any(not ingest.is_measurement(fmt) for fmt, _pattern in req.reports),
        report_error=reports.error, summary=reports.summary,
        allow_empty=req.allow_empty, stderr_tail=stderr[-_STDERR_TAIL:].decode("utf-8", "replace"),
        orphaned_output=outcome.orphaned_output, anomalies=anomalies))
    for flag, limitation in ((outcome.orphaned_output, "orphaned_output"), (outcome.stdout_dropped, "stdout_truncated"),
                             (outcome.stderr_dropped, "stderr_truncated")):
        if flag:
            run.note(limitation)
    streams = {"stdout.log": (_with_marker(stdout, outcome.stdout_dropped), stdout_redactions),
               "stderr.log": (_with_marker(stderr, outcome.stderr_dropped), stderr_redactions)}
    if ran.service_log is not None:
        service_log, service_redactions = run.redactor.bytes_(ran.service_log[0])
        streams["service.log"] = (_with_marker(service_log, ran.service_log[1]), service_redactions)
    evidence_manifest, redactions = _write_evidence(run, directory, streams, timeout_s, reports.manifest)
    record = _execution_data(run, gate, verdict, dict({
        "started_at": outcome.started_at, "ended_at": outcome.ended_at, "duration_ms": outcome.duration_ms,
        "exit_code": outcome.exit_code, "signal": outcome.signal, "timed_out": outcome.timed_out,
        "environment": dict(ran.environment, **{"class": run.environment["class"]}),
        "evidence_manifest": evidence_manifest, "reports": reports.entries, "report": _report_block(reports.summary),
        "redactions": redactions,
    }, **({} if ran.container is None else {"container": ran.container}),
        **({"measurements": list(reports.measurements)} if reports.measurements else {})))
    with run.ws.lock():
        clean = _append(run, "execution.recorded", record)
        for case in reports.summary.cases if reports.summary is not None else ():
            _append(run, "result.recorded", _result_data(case, run.execution_id))
    return _Recorded(verdict, directory.relative_to(run.ws.root).as_posix(), reports.summary, clean["limitations"],
                     reports.measurements)


# --------------------------------------------------------------------------- after the run

def flaky_view(st: dict, execution_id: str) -> Dict[str, dict]:
    root = state.chain_root(st, execution_id)
    return {test_id: {key: chains[root][key] for key in _FLAKY_KEYS}
            for test_id, chains in st["tests"].items() if root in chains}


def _ingest_strength(ws: Workspace, record: dict, file_sha256: str) -> str:
    """``structured`` only for a file the execution's manifest lists, and only while that manifest is intact."""
    recorded = record["evidence_manifest"]
    if recorded is None:
        return "attested"
    try:
        raw = integrity.read_regular(ws.path("evidence", record["id"], "manifest.json"))
    except OSError:
        return "attested"
    if sha256_bytes(raw) != recorded["sha256"]:
        return "attested"
    return "structured" if any(entry["sha256"] == file_sha256 for entry in loads(raw).values()) else "attested"


def ingest_file(ws: Workspace, actor: ledger.Actor, fmt: str, path: Path, *, from_execution: str,
                category: str = "security") -> dict:
    """Attach a report file to an existing execution, as a redacted copy under its ``ingested/``."""
    if schema.errors(category, _CATEGORY):
        raise QaError("invalid_category", "unknown category", category=category)
    record = state.load(ws)["executions"].get(from_execution)
    if record is None:
        raise QaError("unknown_execution", "no such execution", execution=from_execution)
    path = Path(path)
    original = ingest.read_report(path, config.load_config(ws)["defaults"]["max_log_mb"] * _MIB)
    summary = ingest.parse_bytes(fmt, original)
    strength = _ingest_strength(ws, record, sha256_bytes(original))
    redactor = redact.load_redactor(ws)
    clean = redacted_report(redactor, fmt, original)[0]
    digest = sha256_bytes(clean)
    if ingest.is_measurement(fmt):  # what the event records derives from the redacted copy, as a run's does: a path is a dict key
        summary = ingest.parse_bytes(fmt, clean)
    total = {"tests": len(summary.cases), "alerts": len(summary.alerts), "coverage": len(summary.files),
             "mutants": len(summary.mutants)}[summary.kind]
    measured: dict = {}
    if ingest.is_measurement(fmt):  # the numbers are on the event, so a reader need not parse the file again
        measured["totals"] = summary.measurement_totals()
        flags = _flags(ws.root, summary)
        if flags:
            measured["anomalies"] = flags
    with ws.lock():
        folder = ws.path("evidence", from_execution, "ingested")
        folder.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.mkdir(mode=0o700, exist_ok=True)
        name = _stored_name(redactor, str(path), {_name_key(entry.name) for entry in folder.iterdir()})
        write_atomic(folder / name, clean)
        event = ledger.append(ws, actor, "report.ingested", {
            "execution": from_execution, "format": fmt, "kind": summary.kind, "total": total, "file": "ingested/" + name,
            "sha256": digest, "ev_id": ids.ev_id(digest), "strength": strength, "category": category, **measured})
    raised = {"opened": 0, "updated": 0}
    if summary.kind == "alerts":  # §8.2: a standard report becomes results or findings, or the gate never sees it
        raised = findings.ingest_alerts(ws, actor, _redacted(redactor, summary), execution_id=from_execution, category=category)
    return {"event": event["id"], "execution": from_execution, "strength": strength, "kind": summary.kind, "total": total,
            "findings": raised}


def reclassify(ws: Workspace, actor: ledger.Actor, result_id: str, new_status: str, reason: str,
               evidence: List[str]) -> dict:
    """Change a result's status within §8.3's limits, citing evidence the state can resolve."""
    with ws.lock():
        st = state.load(ws)
        result = st["results"].get(result_id)
        why = None
        if result is None:
            why = "unknown_result"
        elif not status.can_reclassify(result["status"], new_status):
            why = "not_allowed"
        elif not isinstance(reason, str) or not reason.strip():
            why = "blank_reason"
        elif not evidence:
            why = "no_evidence"
        elif not set(evidence) <= state.known_references(st):
            why = "unknown_reference"
        if why is not None:
            raise QaError("reclassify_refused", "the reclassification was refused", why=why, result=result_id)
        event = ledger.append(ws, actor, "result.reclassified",
                              {"result": result_id, "new_status": new_status, "reason": reason, "evidence": list(evidence)})
    return {"event": event["id"], "id": result_id, "status": new_status}
