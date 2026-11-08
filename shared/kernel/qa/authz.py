"""Authorization (§12.2): whether an activity may run in an environment, and the grants behind it.

``check`` decides, and the first rule that applies wins:

1. a never-activity (``credential-use``, ``deploy``) is ``policy_denied``;
2. ``db-write`` and ``dast-active`` on a ``production`` environment are ``policy_denied``;
3. a ``ci`` environment is ``policy_denied``: ci environments arrive in milestone 4;
4. ``static-analysis`` is allowed by default;
5. ``test-execution-local`` runs the tests on this machine whatever the environment's class, so
   an untrusted project is ``not_authorized`` on every class (its tests run only in a container,
   §12.2); a trusted one is allowed on a ``local`` environment and otherwise goes on to 6 and 7;
6. a standing authorization in config.json for the environment and the activity allows it;
7. so does a grant that is not revoked, has not expired (it covers a check while
   ``now < expires_at``) and covers the environment, the activity and every host;
8. anything else is ``not_authorized``.

Grants are ``authorization.granted`` events in the Appendix A.4 shape. ``grant`` records them
(``qa authorize``) and ``revoke`` withdraws them (``qa auth revoke``); editing config.json never
creates one. A target is a bare host (with an optional port, or a bracketed IPv6 address) or an
http(s) URL, stored as given. It must name a host, a URL target must carry no userinfo (§12.5),
and it must be a declared host of the grant's own environment. Targets and hosts compare by
hostname, case-insensitively: a URL gives its ``urlsplit`` hostname, as the guard reports hosts,
and a bare host is read as ``config.resolve_host`` reads it. ``write-tests`` paths follow the
rules ``write_scopes`` follow (§12.4), and the stored expiry, given or default, is after the grant time.
"""
from __future__ import annotations

import copy
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, ids, ledger, state, vocab
from .errors import QaError
from .workspace import Workspace

# QaError codes raised here: approval_required, unknown_environment, ci_unsupported,
# invalid_activity, invalid_target, undeclared_target, invalid_limits, invalid_expiry (build_grant,
# and so grant); unknown_authorization, already_revoked (revoke); plus whatever
# config.load_config and ledger.append raise.

_NEVER_ON_PRODUCTION = ("db-write", "dast-active")
_HTTP_URL = re.compile(r"https?://", re.IGNORECASE)
_UNTRUSTED = ("untrusted project: container runs arrive in milestone 2, "
              "or approve `qa config set project.trust trusted`")


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _globs(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_text(v) for v in value)


_Limit = Tuple[str, Callable[[Any], bool], str]  # key, check, what the value must be

# The limits a grant must carry for each activity (§12.2, Appendix A.4); other keys are kept as given.
_LIMITS: Dict[str, Tuple[_Limit, ...]] = {
    "load-test": (("max_vus", _count, "a positive integer"), ("max_rps", _count, "a positive integer"),
                  ("max_duration_s", _count, "a positive integer")),
    "db-write": (("backup_statement", _text, "non-empty text"),),
    "write-tests": (("paths", _globs, "a non-empty list of path globs"),),
    "data-egress": (("service", _text, "non-empty text"), ("data", _text, "non-empty text")),
}
# A load test on production needs explicit, coordinated authorization (§12.2).
_PRODUCTION_LOAD_TEST: Tuple[_Limit, ...] = (("coordination", _text, "non-empty text"),)


@dataclass(frozen=True)
class AuthzDecision:
    allowed: bool
    reason: Optional[str]         # policy_denied | not_authorized when not allowed, else None
    authorization: Optional[str]  # AUTH-NNN, default:static-analysis, trusted:local or standing:<env id>
    detail: str


def _allow(authorization: str, detail: str) -> AuthzDecision:
    return AuthzDecision(True, None, authorization, detail)


def _deny(reason: str, detail: str) -> AuthzDecision:
    return AuthzDecision(False, reason, None, detail)


def _hostname(target: Any) -> Optional[str]:
    """The lower-case name a target or a host compares by; None when it names no host."""
    if not isinstance(target, str):
        return None
    text = target.strip()
    if _HTTP_URL.match(text):
        try:
            name = urllib.parse.urlsplit(text).hostname
        except ValueError:  # e.g. an unclosed IPv6 bracket
            return None
    else:
        name = config._bare_host(text)
    return name or None


# --------------------------------------------------------------------------- decisions

def check(st: dict, cfg: dict, *, activity: str, environment: dict, hosts: Tuple[str, ...] = (),
          now: datetime) -> AuthzDecision:
    """Decide whether ``activity`` may run in ``environment`` on ``hosts``; see the module docstring."""
    env_id, env_class = environment.get("id"), environment.get("class")
    if activity in vocab.NEVER_ACTIVITIES:
        return _deny("policy_denied", "%s is never performed by the suite" % activity)
    if env_class == "production" and activity in _NEVER_ON_PRODUCTION:
        return _deny("policy_denied", "%s is never performed on production" % activity)
    if env_class == "ci":
        return _deny("policy_denied", "ci environments arrive in milestone 4")
    if activity == "static-analysis":
        return _allow("default:static-analysis", "static analysis is allowed by default")
    if activity == "test-execution-local":  # on every class: the tests run on this machine
        if cfg["project"]["trust"] != "trusted":
            return _deny("not_authorized", _UNTRUSTED)
        if env_class == "local":
            return _allow("trusted:local", "the project is trusted to run its tests on this machine")
    for standing in cfg["standing_authorizations"]:
        if standing["environment"] == env_id and activity in standing["activities"]:
            return _allow("standing:%s" % env_id, "standing authorization in config.json")
    names = [_hostname(h) for h in hosts]
    for grant in sorted(st["authorizations"].values(), key=lambda g: int(g["id"][len("AUTH-"):])):
        if _covers(grant, env_id, activity, names, now):
            return _allow(grant["id"], "%s, valid until %s" % (grant["id"], grant["expires_at"]))
    scope = " for %s" % ", ".join(hosts) if hosts else ""
    return _deny("not_authorized",
                 "no standing authorization or valid grant covers %s on %s%s" % (activity, env_id, scope))


def _covers(grant: dict, env_id: Any, activity: str, names: List[Optional[str]], now: datetime) -> bool:
    if grant["revoked_at"] is not None or grant["environment"] != env_id or activity not in grant["activities"]:
        return False
    try:
        if not now < ids.parse_ts(grant["expires_at"]):
            return False
    except QaError:  # fits the timestamp pattern but names no real time
        return False
    targets = {_hostname(t) for t in grant["targets"]}
    return all(name is not None and name in targets for name in names)


def default_expiry(now: datetime) -> datetime:
    """End of ``now``'s local day, 23:59:59.000 local time, as an aware UTC datetime.

    ``mktime`` with ``tm_isdst=-1`` applies the UTC offset in force at 23:59:59, which differs
    from ``now``'s own on the days the clocks change.
    """
    day = time.localtime(now.timestamp())
    end = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 23, 59, 59, 0, 0, -1))
    return datetime.fromtimestamp(end, timezone.utc)


# --------------------------------------------------------------------------- grants

def build_grant(cfg: dict, *, environment_id: str, targets: List[str], activities: List[str], limits: dict,
                approval: str, expires_at: Optional[datetime], now: datetime) -> dict:
    """Check a grant request against ``cfg`` and return its record, every A.4 field but ``id``.

    Refusals, checked in this order: approval_required, unknown_environment, ci_unsupported,
    invalid_activity, invalid_target, undeclared_target, invalid_limits, invalid_expiry.
    ``expires_at`` None means the end of the local day; a given one must be timezone-aware. Given
    or default, the expiry as stored (to the millisecond) must be after ``now``, so in the last
    second of a local day a grant needs a given ``expires_at``.
    """
    if not isinstance(approval, str) or not approval.strip():
        raise QaError("approval_required", "a grant needs the user's approval quote")
    env = next((e for e in cfg["environments"] if e["id"] == environment_id), None)
    if env is None:
        raise QaError("unknown_environment", "config.json declares no such environment", environment=environment_id)
    if env["class"] == "ci":
        raise QaError("ci_unsupported", "ci environments arrive in milestone 4", environment=environment_id)
    _check_activities(env, activities)
    _check_targets(cfg, environment_id, targets)
    _check_limits(env, activities, limits)
    default = expires_at is None
    if default:
        expires_at = default_expiry(now)
    elif not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise QaError("invalid_expiry", "expires_at must be a timezone-aware datetime")
    stored = ids.format_ts(expires_at)
    if not now < ids.parse_ts(stored):  # as check reads it: the grant must cover a check made now
        late = ("the local day ends at %s, so the grant needs a given expires_at" if default
                else "expires_at %s is not after the grant time")
        raise QaError("invalid_expiry", late % stored, expires_at=stored)
    return {
        "environment": environment_id,
        "targets": list(targets),
        "activities": list(activities),
        "limits": copy.deepcopy(limits),
        "granted_by": "user",
        "approval_quote": approval,
        "granted_at": ids.format_ts(now),
        "expires_at": stored,
    }


def _check_activities(env: dict, activities: List[str]) -> None:
    if not activities:
        raise QaError("invalid_activity", "a grant needs at least one activity")
    for activity in activities:
        if activity not in vocab.ACTIVITIES:
            raise QaError("invalid_activity", "unknown activity", activity=activity)
        if activity in vocab.NEVER_ACTIVITIES:
            raise QaError("invalid_activity", "%s is never performed by the suite" % activity, activity=activity)
        if env["class"] == "production" and activity in _NEVER_ON_PRODUCTION:
            raise QaError("invalid_activity", "%s is never performed on production" % activity, activity=activity)


def _target_problem(target: Any) -> Optional[str]:
    """Why ``target`` cannot be a grant target, or None when it can."""
    if _hostname(target) is None:  # empty, or a URL that urlsplit refuses or that has no hostname
        return "a target must name a host"
    text = target.strip()
    if _HTTP_URL.match(text) and "@" in urllib.parse.urlsplit(text).netloc:
        return "a URL target must not carry userinfo: secrets stay out of arguments (§12.5)"
    return None


def _check_targets(cfg: dict, environment_id: str, targets: List[str]) -> None:
    for index, target in enumerate(targets):  # every target names a host before any is resolved
        problem = _target_problem(target)
        if problem is not None:  # the details leave the target out, since it may carry a secret
            raise QaError("invalid_target", problem, index=index, environment=environment_id)
    for target in targets:
        env = config.resolve_host(cfg, _hostname(target))
        if env is None or env["id"] != environment_id:
            raise QaError("undeclared_target", "target is not a declared host of environment %r" % environment_id,
                          target=target, environment=environment_id)


def _check_limits(env: dict, activities: List[str], limits: Any) -> None:
    if not isinstance(limits, dict):
        raise QaError("invalid_limits", "limits must be an object keyed by activity")
    for key, value in limits.items():
        if key not in activities or not isinstance(value, dict):
            raise QaError("invalid_limits", "limits must map activities of the grant to objects", key=key)
    for activity in activities:
        needed = _LIMITS.get(activity, ())
        if activity == "load-test" and env["class"] == "production":
            needed += _PRODUCTION_LOAD_TEST
        given = limits.get(activity, {})
        for key, valid, what in needed:
            if not valid(given.get(key)):
                raise QaError("invalid_limits", "%s needs limit %s.%s, %s" % (activity, activity, key, what),
                              activity=activity, limit=key)
        if activity == "write-tests":  # the rules write_scopes follow (§12.4): never record what is never done
            for path in given["paths"]:
                errors = config._scope_errors(path)
                if errors:
                    raise QaError("invalid_limits", "write-tests.paths follow the write_scopes rules: " + errors[0],
                                  activity=activity, limit="paths", path=path)


def grant(ws: Workspace, actor: ledger.Actor, *, now: Optional[datetime] = None, **build_grant_kwargs: Any) -> dict:
    """Record ``authorization.granted`` and return the stored envelope.

    The AUTH id is taken and the event appended under one workspace lock, so two concurrent
    grants never share an id. ``granted_by`` is always ``"user"``: the grant records the user's
    approval, and the envelope's actor records who ran the command.
    """
    with ws.lock():
        if now is None:
            now = ids.now_utc()
        data = build_grant(config.load_config(ws), now=now, **build_grant_kwargs)
        data["id"] = ledger.next_human_id(ws, "AUTH")
        return ledger.append(ws, actor, "authorization.granted", data)


def revoke(ws: Workspace, actor: ledger.Actor, auth_id: str, reason: str) -> dict:
    """Record ``authorization.revoked`` and return the stored envelope; its ``ts`` is the revocation time.

    The state is read and the event appended under one workspace lock, so a grant is never revoked twice.
    """
    with ws.lock():
        held = state.load(ws)["authorizations"].get(auth_id)
        if held is None:
            raise QaError("unknown_authorization", "no grant has this id", authorization=auth_id)
        if held["revoked_at"] is not None:
            raise QaError("already_revoked", "the grant is already revoked",
                          authorization=auth_id, revoked_at=held["revoked_at"])
        return ledger.append(ws, actor, "authorization.revoked", {"id": auth_id, "reason": reason})
