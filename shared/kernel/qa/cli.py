"""The ``qa`` command line: the one surface skills call (§8.1, §8.2).

Each command prints one JSON object on stdout, on one line with its keys sorted: ``ok`` (true exactly when the exit
code is 0), ``command``, ``session`` and what the command has to say. Full logs go to evidence files.

Exit codes (deviation 20):

    0   success; run passed; gate READY
    1   run failed; gate NOT_READY; ledger verify or report check found problems
    2   usage or input error (every QaError)
    3   run blocked; gate INSUFFICIENT_EVIDENCE; guard check deny or require_grant
    4   run inconclusive
    5   run not_run
    6   gate READY_WITH_CONDITIONS
    70  internal error
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import stat
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from . import authz, config, doctor, execution, findings, gate, guard, ids, ingest, integrity, ledger, redact, report, schema, state, workspace
from .canonical import loads
from .errors import QaError
from .workspace import Workspace, write_atomic

# Commands that record events or write under .qa/. They refuse a workspace a newer schema wrote (§15.7).
WRITE_COMMANDS = (
    "authorize", "auth revoke", "config set", "config accept", "gate policy propose", "gate policy accept", "gate evaluate",
    "run", "ingest", "result reclassify", "finding add", "finding update", "finding state", "state rebuild", "report render",
)

_RUN_EXIT = {"passed": 0, "failed": 1, "blocked": 3, "inconclusive": 4, "not_run": 5}
_GATE_EXIT = {"READY": 0, "NOT_READY": 1, "INSUFFICIENT_EVIDENCE": 3, "READY_WITH_CONDITIONS": 6}
_GUARD_REFUSED = 3
_INPUT_ERROR = 2
_INTERNAL_ERROR = 70
_REPORT_FORMATS = ("md", "json")


class _HelpRequested(Exception):
    """-h or --help, at any level: stdout still carries one JSON object (T18-R7)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _Parser(argparse.ArgumentParser):
    """Refusals as QaError, and everything after the first ``--`` kept away from argparse where it is a command."""

    takes_command = False  # argparse's own ``--`` rules differ between interpreters
    commands: Any = None  # on a parser whose next word is a command, its subparsers action

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        self.color = False  # interpreters from 3.14 on would put terminal escapes in the JSON

    def error(self, message: str) -> None:
        raise QaError("usage", message)

    def print_help(self, file: Any = None) -> None:
        raise _HelpRequested(self.format_help())

    def parse_known_args(self, args: Any = None, namespace: Any = None) -> Any:
        args = list(args)
        end = args.index("--") if "--" in args else len(args)
        if self.takes_command:
            parsed, extras = super().parse_known_args(args[:end], namespace)
            parsed.words = args[end + 1:]
            return parsed, extras
        if self.commands is not None and end < len(args) and not set(args[:end]) & (set(self.commands.choices) | {"-h", "--help"}):
            self.error("the command goes before --")  # newer interpreters would drop this -- and run the command
        return super().parse_known_args(args, namespace)


@dataclass
class Context:
    """What a handler gets besides its arguments, and where it leaves its answer."""
    command: str = ""
    ws: Optional[Workspace] = None
    session: Optional[str] = None
    actor: Optional[ledger.Actor] = None
    limitations: List[str] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)

    def reply(self, **values: Any) -> None:
        self.fields.update(values)

    def body(self, code: int) -> dict:
        body: Dict[str, Any] = {"ok": code == 0, "command": self.command, "session": self.session}
        if self.limitations:
            body["limitations"] = self.limitations
        body.update(self.fields)
        return body


# --------------------------------------------------------------------------- the parser

def seconds(text: str) -> float:
    """--timeout's type: a number JSON can carry, because a refusal echoes it and ``nan`` could not be printed."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def _enum(name: str) -> List[str]:
    return schema.load_schema("defs.schema.json")["$defs"][name]["enum"]


def _command(groups: Dict[str, Any], name: str, text: str) -> _Parser:
    """Register the command ``name``; its words below the last are groups already in ``groups``."""
    parent, _, word = name.rpartition(" ")
    leaf = groups[parent].add_parser(word, help=text, description=text)
    leaf.set_defaults(command=name)
    return leaf


def _group(groups: Dict[str, Any], name: str, text: str) -> None:
    parent, _, word = name.rpartition(" ")
    group = groups[parent].add_parser(word, help=text, description=text)
    groups[name] = group.commands = group.add_subparsers(dest="word", metavar="COMMAND", required=True)


def _taking_command(leaf: _Parser) -> _Parser:
    leaf.takes_command = True
    leaf.add_argument("--shell", action="store_true", help="the one argument after -- is a shell string")
    leaf.add_argument("--activity", default=execution.RunRequest.activity, help="the activity this command is")
    return leaf


def _add_safety_commands(groups: Dict[str, Any]) -> None:
    _group(groups, "guard", "classify a command without running it")
    _taking_command(_command(groups, "guard check", "say whether a command would be allowed, denied or need a grant"))
    grant = _command(groups, "authorize", "record a scoped, expiring authorization the user approved")
    grant.add_argument("--environment", required=True, metavar="E")
    grant.add_argument("--activity", required=True, action="extend", nargs="+", metavar="A")
    grant.add_argument("--target", action="extend", nargs="+", metavar="H", default=[],
                       help="a host the grant covers; only hosts declared in the configuration can be named, "
                            "so a third-party host needs an environment entry first")
    grant.add_argument("--limit", action="append", metavar="ACTIVITY.KEY=VALUE", default=[])
    grant.add_argument("--approval", required=True, metavar="Q", help="the user's approval, quoted")
    grant.add_argument("--expires", metavar="TS", help="YYYY-MM-DDTHH:MM:SS.mmmZ (default: the end of the local day)")
    _group(groups, "auth", "inspect and withdraw authorizations")
    _command(groups, "auth list", "print the authorizations")
    revoke = _command(groups, "auth revoke", "withdraw an authorization")
    revoke.add_argument("authorization", metavar="AUTH")
    revoke.add_argument("--reason", required=True, metavar="R")


def _add_config_commands(groups: Dict[str, Any]) -> None:
    _group(groups, "config", "read and change the configuration")
    _command(groups, "config show", "print the configuration, its hash and the accepted hash")
    change = _command(groups, "config set", "change one setting with the user's approval")
    change.add_argument("key", metavar="KEY")
    change.add_argument("value", metavar="VALUE", help="read as JSON when it parses, else kept as text")
    change.add_argument("--approval", required=True, metavar="Q", help="the user's approval, quoted")
    accept = _command(groups, "config accept", "accept config.json as it stands, with the user's approval")
    accept.add_argument("--approval", required=True, metavar="Q", help="the user's approval, quoted")


def _add_gate_commands(groups: Dict[str, Any]) -> None:
    _group(groups, "gate", "evaluate the gate and change its policy")
    _group(groups, "gate policy", "propose and accept gate overrides")
    propose = _command(groups, "gate policy propose", "propose an override of one criterion; it changes nothing yet")
    propose.add_argument("criterion", metavar="CRITERION")
    propose.add_argument("--override", required=True, metavar="JSON")
    propose.add_argument("--rationale", required=True, metavar="R")
    accept = _command(groups, "gate policy accept", "write a proposed override into config.json, with the user's approval")
    accept.add_argument("event", metavar="EVT")
    accept.add_argument("--approval", required=True, metavar="Q", help="the user's approval, quoted")
    evaluate = _command(groups, "gate evaluate", "evaluate a profile against the ledger and record the verdict")
    evaluate.add_argument("--profile", metavar="P")
    evaluate.add_argument("--target", metavar="COMMIT", help="the full 40-character commit hash")


def _add_execution_commands(groups: Dict[str, Any]) -> None:
    run = _taking_command(_command(groups, "run", "run one command under the guard and record the evidence"))
    run.add_argument("--report", action="append", metavar="FORMAT:PATH", default=[],
                     help="a report the command writes; {evidence} is its evidence directory")
    run.add_argument("--timeout", type=seconds, metavar="S", help="seconds (default: defaults.timeout_seconds)")
    run.add_argument("--attempt-of", metavar="EXE", help="the execution this one repeats")
    run.add_argument("--environment", default=execution.RunRequest.environment, metavar="E")
    run.add_argument("--scope", choices=("targeted", "full"), default=execution.RunRequest.scope)
    run.add_argument("--category", default=execution.RunRequest.category, metavar="C")
    run.add_argument("--allow-empty", action="store_true", help="a report with no tests is not inconclusive")
    attach = _command(groups, "ingest", "attach a report file to a recorded execution")
    attach.add_argument("format", choices=("junit", "ctrf", "sarif"), metavar="FORMAT")
    attach.add_argument("file", metavar="FILE")
    attach.add_argument("--from", dest="from_execution", required=True, metavar="EXE")
    attach.add_argument("--category", metavar="C", help="the result category (default: security for sarif, else functional)")
    _group(groups, "result", "change recorded results")
    reclassify = _command(groups, "result reclassify", "change a result's status within the limits, citing evidence")
    reclassify.add_argument("result", metavar="RES")
    reclassify.add_argument("status", metavar="STATUS")
    reclassify.add_argument("--reason", required=True, metavar="R")
    reclassify.add_argument("--evidence", required=True, action="extend", nargs="+", metavar="ID")


def _add_finding_commands(groups: Dict[str, Any]) -> None:
    _group(groups, "finding", "record findings and move them along their lifecycle")
    add = _command(groups, "finding add", "record a finding, or a new sighting of one already open")
    add.add_argument("--json", dest="json_file", required=True, metavar="FILE")
    update = _command(groups, "finding update", "change a finding's fields")
    update.add_argument("finding", metavar="FND")
    update.add_argument("--json", dest="json_file", required=True, metavar="FILE")
    change = _command(groups, "finding state", "move a finding along its lifecycle")
    change.add_argument("finding", metavar="FND")
    change.add_argument("state", metavar="STATE")
    change.add_argument("--reason", required=True, metavar="R")
    change.add_argument("--accepted-by", metavar="WHO", help="for accepted_risk: who accepted it")
    change.add_argument("--expires-at", metavar="TS", help="for accepted_risk: YYYY-MM-DDTHH:MM:SS.mmmZ")
    change.add_argument("--duplicate-of", metavar="FND", help="for duplicate: the finding this one repeats")
    _command(groups, "finding list", "print the findings")
    _group(groups, "rubric", "print the shared rubrics")
    _command(groups, "rubric severity", "print the severity rubric")
    _command(groups, "rubric priority", "print the priority matrix")


def _add_integrity_commands(groups: Dict[str, Any]) -> None:
    _group(groups, "ledger", "verify and read the ledger")
    _command(groups, "ledger verify", "check every chain, event, evidence file and kernel file")
    show = _command(groups, "ledger show", "print the recorded events")
    show.add_argument("--session", dest="only_session", metavar="S", help="only this session")
    show.add_argument("--type", dest="only_type", metavar="T", help="only this event type")
    _group(groups, "state", "derive and read the state")
    _command(groups, "state rebuild", "fold the ledger and write .qa/state/")
    show = _command(groups, "state show", "print the state, or one of its keys")
    show.add_argument("key", nargs="?", metavar="KEY")
    _group(groups, "report", "render reports and check their claims")
    render = _command(groups, "report render", "write the report files for the state as it is now")
    render.add_argument("--format", action="append", choices=_REPORT_FORMATS, metavar="FORMAT", help="md or json (default: both)")
    check = _command(groups, "report check", "check that every claim in a report cites a recorded run")
    check.add_argument("file", metavar="FILE")


def build_parser() -> _Parser:
    parser = _Parser(prog="qa", description="Evidence-recording QA kernel. Every command prints one JSON object on stdout.")
    parser.add_argument("--workspace", metavar="PATH", help="the project root holding .qa/ (default: search upward from here)")
    parser.add_argument("--session", metavar="S-...", help="session ID (default: $QA_SESSION, then .qa/default-session)")
    parser.add_argument("--role", choices=_enum("role"), default="unspecified", help="the role recorded as actor")
    parser.add_argument("--skill", help="the skill recorded as actor")
    parser.add_argument("--independence", choices=_enum("independence"), default="I1", help="the independence level; run refuses I0")
    groups = {"": parser.add_subparsers(dest="word", metavar="COMMAND", required=True)}
    parser.commands = groups[""]
    _command(groups, "init", "create .qa/ here or at --workspace")
    _command(groups, "doctor", "report on the kernel, the ledger, the configuration and this machine's tools")
    _add_safety_commands(groups)
    _add_config_commands(groups)
    _add_gate_commands(groups)
    _add_execution_commands(groups)
    _add_finding_commands(groups)
    _add_integrity_commands(groups)
    return parser


# --------------------------------------------------------------------------- input helpers

def _recorded(event: dict) -> Dict[str, str]:
    """``event``, and ``id`` when the record has its own ID."""
    own = event["data"].get("id")
    return {"event": event["id"], "id": own} if own is not None else {"event": event["id"]}


def _command_words(args: argparse.Namespace) -> Union[List[str], str]:
    """The words after ``--``, or with --shell the one shell string: joining several would lose their quoting."""
    if not args.words:
        raise QaError("usage", "the command goes after --")
    if not args.shell:
        return args.words
    if len(args.words) != 1:
        raise QaError("usage", "--shell takes exactly one argument after --, the shell string")
    return args.words[0]


def _working_directory(ws: Workspace) -> Optional[Path]:
    """Where this process really is, when that is inside the workspace; None means the workspace root (T18-R1)."""
    try:
        root, here = os.path.realpath(str(ws.root)), os.path.realpath(os.getcwd())
        inside = os.path.commonpath([root, here]) == root
    except (OSError, ValueError):  # removed while we ran, or on another drive
        return None
    return Path(here) if inside else None


def _report_pair(text: str) -> Tuple[str, str]:
    fmt, colon, pattern = text.partition(":")
    if not (colon and fmt and pattern):
        raise QaError("usage", "--report takes FORMAT:PATH", report=text)
    return fmt, pattern


def _limits(items: List[str]) -> Dict[str, dict]:
    """``--limit ACTIVITY.KEY=VALUE`` as A.4's per-activity map; a VALUE is read as ``config set`` reads one."""
    limits: Dict[str, dict] = {}
    for item in items:
        activity, dot, rest = item.partition(".")
        key, equals, value = rest.partition("=")
        if not (dot and equals and activity and key):
            raise QaError("usage", "--limit takes ACTIVITY.KEY=VALUE")
        limits.setdefault(activity, {})[key] = config._parse_value(value)
    return limits


def _refuse_irregular(path: str) -> None:
    """Reading a report takes the whole file: a FIFO would hang it and a device would never end."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return  # parse_report says what is wrong with it
    if not stat.S_ISREG(mode):
        raise ingest.ReportInvalid("unreadable", "not a regular file", path=path)


def _read_file(path: str) -> bytes:
    """``path``'s bytes; anything but a regular file is refused, because a FIFO would hang the read and a device never end it."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("not a regular file")
            with open(fd, "rb", closefd=False) as handle:
                return handle.read()
        finally:
            os.close(fd)
    except OSError as exc:
        raise QaError("file_unreadable", "the file cannot be read", path=path, error=str(exc))


def _json_object(data: Union[bytes, str]) -> dict:
    value = loads(data)
    if not isinstance(value, dict):
        raise QaError("invalid_json", "expected a JSON object")
    return value


# --------------------------------------------------------------------------- the commands

def cmd_init(args: argparse.Namespace, ctx: Context) -> int:
    try:
        ctx.ws, created = workspace.init_workspace(Path(args.workspace) if args.workspace is not None else Path.cwd())
    except OSError as exc:
        raise QaError("invalid_workspace", "the workspace cannot be created there", error=str(exc))
    _bind_actor(args, ctx)
    if created:
        ctx.reply(event=config.record_initialized(ctx.ws, ctx.actor)["id"])
    ctx.limitations.extend(integrity.authority(ctx.ws)["refused"])  # a clone may arrive with .qa/ already in it (§12.2)
    ctx.reply(created=created, workspace=str(ctx.ws.root))
    return 0


def cmd_doctor(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(**doctor.doctor(ctx.ws))
    return 0


def cmd_guard_check(args: argparse.Namespace, ctx: Context) -> int:
    # A newer major may hold fields this kernel's schema refuses; a read still answers, flagged schema_newer (§15.7).
    cfg = loads(ctx.ws.path("config.json").read_bytes()) if "schema_newer" in ctx.limitations else config.load_config(ctx.ws)
    decision = guard.classify(_command_words(args), shell=args.shell, ws_root=ctx.ws.root, qa_dir=ctx.ws.qa_dir,
                              config=cfg, declared_activity=args.activity, cwd=_working_directory(ctx.ws))
    ctx.reply(**dataclasses.asdict(decision))
    return 0 if decision.decision == "allow" else _GUARD_REFUSED


def cmd_authorize(args: argparse.Namespace, ctx: Context) -> int:
    expires = None if args.expires is None else ids.parse_ts(args.expires)
    event = authz.grant(ctx.ws, ctx.actor, environment_id=args.environment, targets=args.target, activities=args.activity,
                        limits=_limits(args.limit), approval=args.approval, expires_at=expires)
    if config.current_hash(ctx.ws) != config.accepted_hash(state.load(ctx.ws)):
        ctx.limitations.append("config_drift")  # recorded, but not usable until the file is accepted
    ctx.reply(**_recorded(event))
    return 0


def cmd_auth_list(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(authorizations=state.load(ctx.ws)["authorizations"])
    return 0


def cmd_auth_revoke(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(**_recorded(authz.revoke(ctx.ws, ctx.actor, args.authorization, args.reason)))
    return 0


def cmd_config_show(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(config=loads(ctx.ws.path("config.json").read_bytes()), hash=config.current_hash(ctx.ws),
              accepted_hash=config.accepted_hash(state.load(ctx.ws)))
    return 0


def cmd_config_set(args: argparse.Namespace, ctx: Context) -> int:
    event = config.set_value(ctx.ws, ctx.actor, args.key, args.value, args.approval)
    ctx.reply(hash=event["data"]["hash"], **_recorded(event))
    return 0


def cmd_config_accept(args: argparse.Namespace, ctx: Context) -> int:
    event = config.accept(ctx.ws, ctx.actor, args.approval)
    ctx.reply(hash=event["data"]["hash"], **_recorded(event))
    return 0


def cmd_gate_policy_propose(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(**_recorded(gate.propose(ctx.ws, ctx.actor, args.criterion, _json_object(args.override), args.rationale)))
    return 0


def cmd_gate_policy_accept(args: argparse.Namespace, ctx: Context) -> int:
    event = gate.accept(ctx.ws, ctx.actor, args.event, args.approval)
    ctx.reply(hash=event["data"]["config_hash"], **_recorded(event))
    return 0


def cmd_gate_evaluate(args: argparse.Namespace, ctx: Context) -> int:
    event = gate.evaluate(ctx.ws, ctx.actor, profile=args.profile, target_commit=args.target)
    data = event["data"]
    ctx.reply(verdict=data["verdict"], profile=data["profile"], criteria=data["criteria"], conditions=data["conditions"], **_recorded(event))
    return _GATE_EXIT[data["verdict"]]


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    if ctx.actor.independence == "I0":
        raise QaError("independence_too_low", "a run needs independence I1 or higher")
    request = execution.RunRequest(
        command=_command_words(args), shell=args.shell, cwd=_working_directory(ctx.ws),
        reports=tuple(_report_pair(text) for text in args.report),
        timeout_s=args.timeout, attempt_of=args.attempt_of, activity=args.activity, environment=args.environment,
        scope=args.scope, allow_empty=args.allow_empty, category=args.category)
    result = execution.execute(ctx.ws, ctx.actor, request)
    ctx.reply(**result)
    return _RUN_EXIT[result["status"]]


def cmd_ingest(args: argparse.Namespace, ctx: Context) -> int:
    _refuse_irregular(args.file)
    category = args.category if args.category is not None else "security" if args.format == "sarif" else "functional"
    ctx.reply(**execution.ingest_file(ctx.ws, ctx.actor, args.format, Path(args.file), from_execution=args.from_execution,
                                      category=category))
    return 0


def cmd_result_reclassify(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(**execution.reclassify(ctx.ws, ctx.actor, args.result, args.status, args.reason, args.evidence))
    return 0


def cmd_finding_add(args: argparse.Namespace, ctx: Context) -> int:
    event, created = findings.add(ctx.ws, ctx.actor, _json_object(_read_file(args.json_file)))
    ctx.reply(created=created, **_recorded(event))
    return 0


def cmd_finding_update(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(**_recorded(findings.update(ctx.ws, ctx.actor, args.finding, _json_object(_read_file(args.json_file)))))
    return 0


def cmd_finding_state(args: argparse.Namespace, ctx: Context) -> int:
    event = findings.change_state(ctx.ws, ctx.actor, args.finding, args.state, reason=args.reason, accepted_by=args.accepted_by,
                                  expires_at=args.expires_at, duplicate_of=args.duplicate_of)
    ctx.reply(state=event["data"]["to"], **_recorded(event))
    return 0


def cmd_finding_list(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(findings=state.load(ctx.ws)["findings"])
    return 0


def cmd_rubric_severity(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(rubric=findings.load_rubric())
    return 0


def cmd_rubric_priority(args: argparse.Namespace, ctx: Context) -> int:
    ctx.reply(matrix=findings.load_matrix())
    return 0


def cmd_ledger_verify(args: argparse.Namespace, ctx: Context) -> int:
    problems = doctor.ledger_problems(ctx.ws)
    ctx.reply(problems=problems)
    return 1 if problems else 0


def cmd_ledger_show(args: argparse.Namespace, ctx: Context) -> int:
    events = (list(ledger.iter_session(ctx.ws, args.only_session)) if args.only_session is not None
              else ledger.iter_all_events(ctx.ws))
    ctx.reply(events=[e for e in events if args.only_type in (None, e["type"])])
    return 0


def cmd_state_rebuild(args: argparse.Namespace, ctx: Context) -> int:
    state.rebuild(ctx.ws)
    ctx.reply(summary=loads(ctx.ws.path("state", "summary.json").read_bytes()))
    return 0


def cmd_state_show(args: argparse.Namespace, ctx: Context) -> int:
    st = state.load(ctx.ws)
    if args.key is not None and args.key not in st:
        raise QaError("unknown_key", "the state has no such key", key=args.key, known=sorted(st))
    ctx.reply(state=st if args.key is None else st[args.key])
    return 0


def cmd_report_render(args: argparse.Namespace, ctx: Context) -> int:
    event = report.render(ctx.ws, ctx.actor, formats=tuple(args.format or _REPORT_FORMATS))
    ctx.reply(files=event["data"]["files"], **_recorded(event))
    return 0


def cmd_report_check(args: argparse.Namespace, ctx: Context) -> int:
    try:
        text = _read_file(args.file).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QaError("file_unreadable", "the file is not UTF-8", path=args.file, error=str(exc))
    problems = [problem._asdict() for problem in report.check_markdown(text, state.load(ctx.ws))]
    ctx.reply(problems=problems)
    return 1 if problems else 0


# --------------------------------------------------------------------------- dispatch

def _given_session(args: argparse.Namespace) -> Optional[str]:
    """--session, else $QA_SESSION; None when neither was given."""
    given = args.session if args.session is not None else os.environ.get("QA_SESSION")
    return None if given is None else ledger.validate_session_id(given)


def _default_session(ws: Workspace) -> str:
    try:
        if not ws.path("default-session").is_file():  # a FIFO would hang the read
            raise OSError("not a regular file")
        return workspace.read_default_session(ws)
    except (OSError, UnicodeDecodeError) as exc:
        raise QaError("invalid_session", "default-session cannot be read", error=type(exc).__name__)


def _bind_actor(args: argparse.Namespace, ctx: Context) -> None:
    ctx.session = _given_session(args) or _default_session(ctx.ws)
    ctx.actor = ledger.Actor(role=args.role, skill=args.skill, session=ctx.session, independence=args.independence)


def _check_schema(args: argparse.Namespace, ctx: Context) -> None:
    try:
        limitation = workspace.check_schema_version(ctx.ws, for_write=args.command in WRITE_COMMANDS)
    except OSError as exc:
        raise QaError("config_invalid", "config.json cannot be read", errors=[type(exc).__name__])
    if limitation is not None:
        ctx.limitations.append(limitation)


def _enter_workspace(args: argparse.Namespace, ctx: Context) -> None:
    try:
        ctx.ws = workspace.find_workspace(Path("."), Path(args.workspace) if args.workspace is not None else None)
    except OSError as exc:  # the working directory was removed
        raise QaError("no_workspace", "the workspace cannot be reached", error=type(exc).__name__)
    for step in (_check_schema, _bind_actor):
        try:
            step(args, ctx)
        except QaError:
            if args.command != "doctor":  # a broken project is what doctor reports
                raise


def _run(argv: List[str], ctx: Context) -> Tuple[int, dict]:
    try:
        args = build_parser().parse_args(argv)
        ctx.command = args.command
        _given_session(args)  # refused before anything is read or created
        (args.skill or "").encode("utf-8")  # the ledger could not store it, and a run must not start first
        if args.command != "init":
            _enter_workspace(args, ctx)
        code = globals()["cmd_" + args.command.replace(" ", "_")](args, ctx)
    except _HelpRequested as shown:
        return 0, {"ok": True, "command": "help", "help": shown.text}
    except QaError as exc:
        return _INPUT_ERROR, {"ok": False, "error": exc.to_json()}
    except UnicodeEncodeError:  # an argument or a JSON escape holding a surrogate reached the ledger
        return _INPUT_ERROR, {"ok": False, "error": QaError("not_encodable", "text UTF-8 cannot encode cannot be recorded").to_json()}
    return code, ctx.body(code)


def _crash_log(ws: Optional[Workspace]) -> Optional[str]:
    """Write the exception being handled, redacted, under .qa/work/; None when there is no way to (T18-R9)."""
    if ws is None:
        return None
    try:
        text = redact.load_redactor(ws).text(traceback.format_exc())[0]
        path = ws.path("work", "crash-%s.log" % ids.ulid())
        write_atomic(path, text.encode("utf-8", "backslashreplace"))
    except Exception:
        return None
    return str(path)


def _encode(body: dict) -> bytes:
    """Canonical JSON whose strings UTF-8 cannot encode, such as an echoed non-UTF-8 argument, are escaped (T18-R6)."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8", "backslashreplace")


def main(argv: Optional[List[str]] = None) -> int:
    ctx = Context()
    try:
        code, body = _run(sys.argv[1:] if argv is None else list(argv), ctx)
        data = _encode(body)
    except Exception:
        code, data = _INTERNAL_ERROR, _encode({"ok": False, "error": {"code": "internal", "crash_log": _crash_log(ctx.ws)}})
    stream = sys.stdout.buffer  # bytes, so the locale cannot break the output
    stream.write(data + b"\n")
    stream.flush()
    return code
