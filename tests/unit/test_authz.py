from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from qa import authz, config, ids, ledger, redact, state, workspace
from qa.errors import QaError

LOCAL = workspace.default_config("x")["environments"][0]
NOON = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
STAGING = {"id": "staging", "class": "staging", "base_urls": ["https://staging.shop.example"]}
PROD = {"id": "prod", "class": "production", "base_urls": ["https://shop.example"]}
EPH = {"id": "eph", "class": "ephemeral", "ephemeral_basis": "user-declared", "base_urls": []}
CI = {"id": "ci", "class": "ci"}
LOAD = {"max_vus": 50, "max_rps": 100, "max_duration_s": 600}
UNTRUSTED = "untrusted project: container runs arrive in milestone 2, or approve `qa config set project.trust trusted`"


def chk(st, cfg, activity, env=LOCAL, hosts=()):
    return authz.check(st, cfg, activity=activity, environment=env, hosts=hosts, now=ids.now_utc())


def _cfg(*environments):
    cfg = workspace.default_config("x")
    cfg["environments"].extend(environments)
    return cfg


def _build(cfg=None, **changes):
    request = dict(environment_id="local", targets=["localhost"], activities=["db-readonly"], limits={},
                   approval="ok", expires_at=None, now=NOON)
    request.update(changes)
    return authz.build_grant(_cfg() if cfg is None else cfg, **request)


def _refusal(cfg=None, **changes):
    with pytest.raises(QaError) as e:
        _build(cfg, **changes)
    return e.value.code


def _grant(ws, actor, **changes):
    request = dict(environment_id="local", targets=[], activities=["db-readonly"], limits={},
                   approval="ok", expires_at=None)
    request.update(changes)
    return authz.grant(ws, actor, **request)


def _granted(auth_id, **changes):
    """authorization.granted data appended straight to the ledger, as a hand-made line would be."""
    data = {"id": auth_id, "environment": "local", "targets": [], "activities": ["db-readonly"], "limits": {},
            "granted_by": "user", "approval_quote": "ok", "granted_at": "2026-11-07T12:00:00.000Z",
            "expires_at": "2026-11-07T23:59:59.000Z"}
    data.update(changes)
    return data


@pytest.fixture
def restore_tz():
    """Put TZ back after a test changes it.

    monkeypatch restores TZ only after this finalizer has run, so the finalizer restores TZ
    itself before calling tzset; otherwise tzset would keep the test's zone for later tests.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is not available on Windows")
    saved = os.environ.get("TZ")
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


# --------------------------------------------------------------------------- the brief's tests

@pytest.mark.parametrize("activity,env_class,reason", [("deploy", "local", "policy_denied"), ("credential-use", "local", "policy_denied"),
                                                       ("db-write", "production", "policy_denied"), ("dast-active", "production", "policy_denied")])
def test_policy_denials(activity, env_class, reason):
    d = chk(state.empty_state(), workspace.default_config("x"), activity, {"id": "e", "class": env_class, "base_urls": []})
    assert not d.allowed and d.reason == reason


def test_static_analysis_default():
    assert chk(state.empty_state(), workspace.default_config("x"), "static-analysis").authorization == "default:static-analysis"


def test_untrusted_local_tests_not_authorized_trusted_allowed():
    cfg = workspace.default_config("x")
    assert chk(state.empty_state(), cfg, "test-execution-local").reason == "not_authorized"
    cfg["project"]["trust"] = "trusted"
    assert chk(state.empty_state(), cfg, "test-execution-local").authorization == "trusted:local"


def test_standing_authorization():
    cfg = workspace.default_config("x")
    cfg["standing_authorizations"] = [{"environment": "local", "activities": ["install-dependencies"]}]
    assert chk(state.empty_state(), cfg, "install-dependencies").authorization == "standing:local"


def test_grant_used_then_expires(ws, actor, frozen_now):
    frozen_now.set(datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))   # T10-R1: never the last second of a local day
    cfg = config.load_config(ws)
    g = authz.grant(ws, actor, environment_id="local", targets=["localhost"], activities=["load-test"],
                    limits={"load-test": {"max_vus": 5, "max_rps": 10, "max_duration_s": 60}}, approval="ok for 1h", expires_at=None)
    assert (g["data"]["granted_by"], g["data"]["limits"]["load-test"]["max_vus"]) == ("user", 5)
    assert g["data"]["id"] == "AUTH-001" and chk(state.load(ws), cfg, "load-test", hosts=("localhost",)).authorization == "AUTH-001"
    frozen_now.advance(86400)
    assert chk(state.load(ws), cfg, "load-test").reason == "not_authorized"


def test_revoked_grant_not_used(ws, actor, frozen_now):
    frozen_now.set(NOON)   # T10-R1: with T10-R7, a default-expiry grant in the last second of a local day is refused
    authz.grant(ws, actor, environment_id="local", targets=["localhost"], activities=["db-readonly"], limits={}, approval="ok", expires_at=None)
    authz.revoke(ws, actor, "AUTH-001", "done")
    assert chk(state.load(ws), config.load_config(ws), "db-readonly", hosts=("localhost",)).reason == "not_authorized"


@pytest.mark.parametrize("kwargs,code", [
    (dict(activities=["load-test"], limits={}), "invalid_limits"),
    (dict(activities=["load-test"], limits={"max_vus": 5, "max_rps": 10, "max_duration_s": 60}), "invalid_limits"),   # flat, not keyed by activity
    (dict(activities=["deploy"], limits={}), "invalid_activity"),
    (dict(activities=["db-readonly"], limits={}, targets=["db.example.com"]), "undeclared_target"),
    (dict(activities=["db-readonly"], limits={}, approval=""), "approval_required"),
])
def test_build_grant_refusals(kwargs, code):
    base = dict(environment_id="local", targets=["localhost"], approval="ok", expires_at=None, now=ids.now_utc())
    with pytest.raises(QaError) as e:
        authz.build_grant(workspace.default_config("x"), **{**base, **kwargs})
    assert e.value.code == code


def test_ci_environment_refused():
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "ci", "class": "ci"})
    d = chk(state.empty_state(), cfg, "static-analysis", {"id": "ci", "class": "ci"})   # step (3) comes before the static-analysis default
    assert not d.allowed and d.reason == "policy_denied" and "milestone 4" in d.detail
    with pytest.raises(QaError) as e:
        authz.build_grant(cfg, environment_id="ci", targets=[], activities=["db-readonly"], limits={}, approval="ok", expires_at=None, now=ids.now_utc())
    assert e.value.code == "ci_unsupported"


def test_default_expiry_is_local_end_of_day(monkeypatch, restore_tz):
    monkeypatch.setenv("TZ", "America/Bogota")
    time.tzset()
    if time.timezone != 18000:   # T10-R4: without tzdata, glibc falls back to UTC without an error
        pytest.skip("tz database missing")
    now = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    assert ids.format_ts(authz.default_expiry(now)) == "2026-11-08T04:59:59.000Z"


def test_default_expiry_on_the_day_the_clocks_go_back(monkeypatch, restore_tz):
    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")   # a POSIX rule, so no tz database is needed
    time.tzset()
    now = datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)   # 01:00 EDT; the clocks go back to EST at 02:00
    end = authz.default_expiry(now)
    assert ids.format_ts(end) == "2026-11-02T04:59:59.000Z" and end.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------- the record (A.4, F22, T10 note)

def test_grant_records_the_a4_shape(ws, actor, frozen_now):
    frozen_now.set(NOON)
    e = authz.grant(ws, actor, environment_id="local", targets=["http://localhost:8080"], activities=["dast-passive"],
                    limits={}, approval="Yes, run the ZAP baseline on localhost today.", expires_at=None)
    assert e["type"] == "authorization.granted" and e["id"].startswith("EVT-")
    assert e["data"] == {
        "id": "AUTH-001", "environment": "local", "targets": ["http://localhost:8080"], "activities": ["dast-passive"],
        "limits": {}, "granted_by": "user", "approval_quote": "Yes, run the ZAP baseline on localhost today.",
        "granted_at": "2026-11-07T12:00:00.000Z", "expires_at": ids.format_ts(authz.default_expiry(NOON)),
    }
    assert state.load(ws)["authorizations"] == {"AUTH-001": {**e["data"], "revoked_at": None, "revoke_reason": None}}
    assert _grant(ws, actor)["data"]["id"] == "AUTH-002"


def test_grant_takes_an_explicit_now(ws, actor, frozen_now):
    frozen_now.set(NOON + timedelta(hours=3))
    g = _grant(ws, actor, now=NOON)
    assert (g["data"]["granted_at"], g["data"]["expires_at"]) == ("2026-11-07T12:00:00.000Z", ids.format_ts(authz.default_expiry(NOON)))


def test_a_given_expiry_is_stored_in_utc():
    later = datetime(2026, 11, 7, 20, 30, tzinfo=timezone(timedelta(hours=2)))
    assert _build(expires_at=later)["expires_at"] == "2026-11-07T18:30:00.000Z"


@pytest.mark.parametrize("expires_at", [datetime(2026, 11, 7, 18, 0), NOON, NOON - timedelta(seconds=1),
                                        "2026-11-07T18:00:00.000Z", NOON + timedelta(microseconds=500)])
def test_invalid_expiry(expires_at):
    # naive, equal to now, before now, not a datetime, and stored as now (T10-R7: the stored expiry has milliseconds)
    assert _refusal(expires_at=expires_at) == "invalid_expiry"


def test_a_default_expiry_that_is_not_after_now_is_refused(ws, actor, frozen_now, monkeypatch, restore_tz):
    monkeypatch.setenv("TZ", "UTC0")   # T10-R7: pin the zone, so these times are local too
    time.tzset()
    last = datetime(2026, 11, 7, 23, 59, 59, 500000, tzinfo=timezone.utc)
    assert _refusal(now=last) == "invalid_expiry"
    assert _refusal(now=last.replace(microsecond=0)) == "invalid_expiry"   # the end of the day is not after it
    assert _build(now=last - timedelta(milliseconds=501))["expires_at"] == "2026-11-07T23:59:59.000Z"
    frozen_now.set(last)
    with pytest.raises(QaError) as e:
        _grant(ws, actor)
    assert e.value.code == "invalid_expiry"
    assert "authorization.granted" not in [event["type"] for event in ledger.iter_all_events(ws)]


# --------------------------------------------------------------------------- targets (T10-R2)

def test_targets_may_be_hosts_or_http_urls_and_are_stored_as_given():
    targets = ["localhost", "LocalHost:3000", "[::1]:8080", "127.0.0.1", "http://LOCALHOST:8080/app", "HTTPS://[::1]/"]
    assert _build(targets=targets)["targets"] == targets
    staging = ["https://Staging.Shop.Example/checkout", "staging.shop.example:443"]
    assert _build(_cfg(STAGING), environment_id="staging", targets=staging)["targets"] == staging


@pytest.mark.parametrize("target", ["db.example.com", "https://staging.shop.example", "staging.shop.example:443",
                                    "127.0.0.2", "[::2]:8080", "::2", "fe80::1%eth0"])
def test_a_target_must_be_a_declared_host_of_the_grant_environment(target):
    assert _refusal(_cfg(STAGING), targets=[target]) == "undeclared_target"   # a staging host is not a local one


@pytest.mark.parametrize("target", ["localhost", "LOCALHOST:8080", "127.0.0.1", "[::1]", "[::1]:8080", "::1"])
def test_bare_targets_of_the_three_forms_are_accepted(target):
    assert _build(targets=[target])["targets"] == [target]   # T10-R10, on the default config


@pytest.mark.parametrize("target", [
    "", "  ", "http://[::1", "http://", "https://:8080/",                           # names no host (T10-R6)
    "http://admin@localhost", "http://admin:hunter22x@localhost:8080/", "HTTP://@localhost/",   # userinfo
    "ftp://localhost", "localhost://x",                                             # a URL that is not http(s)
    "admin@localhost", "localhost/app", "http:/localhost",                          # T10-R10: '@' or '/'
    " localhost", "localhost ", "local host", "localhost\n",                         # whitespace
    "localhost:abc", "localhost:", "localhost:123456", ":8080",                     # no 1-5 digit port, no name
    "[localhost]", "[127.0.0.1]", "[::1]x", "[::1]:", "fe80::1%a@b",                 # no IPv6, a zone of no name
])
def test_a_target_must_be_a_bare_host_or_an_http_url(target):
    with pytest.raises(QaError) as e:
        _build(targets=["localhost", target])
    assert (e.value.code, e.value.details) == ("invalid_target", {"index": 1})   # T10-R10: the index, never the text
    assert not target.strip() or target not in e.value.message


@pytest.mark.parametrize("target,secret", [("localhost:hunter22x@evil.example", "hunter22x"),
                                           ("admin:pw@localhost", "admin:pw")])
def test_a_bare_target_with_userinfo_is_refused_without_echoing_it(target, secret):
    with pytest.raises(QaError) as e:
        _build(targets=[target])
    assert (e.value.code, e.value.details) == ("invalid_target", {"index": 0})
    assert secret not in json.dumps(e.value.to_json()) and secret not in str(e.value)


def test_an_undeclared_target_is_refused_with_its_index_only():
    with pytest.raises(QaError) as e:
        _build(targets=["localhost", "db.example.com"])
    assert (e.value.code, e.value.details) == ("undeclared_target", {"index": 1})
    assert "db.example.com" not in json.dumps(e.value.to_json())


def test_a_stored_target_of_no_target_form_covers_nothing(ws, actor, frozen_now):
    frozen_now.set(NOON)   # hand-made grant data: build_grant refuses the first two targets
    ledger.append(ws, actor, "authorization.granted",
                  _granted("AUTH-001", targets=["localhost:pw@evil.example", "http://admin@localhost/", "127.0.0.1"]))
    st, cfg = state.load(ws), config.load_config(ws)
    assert chk(st, cfg, "db-readonly", hosts=("127.0.0.1",)).authorization == "AUTH-001"
    assert chk(st, cfg, "db-readonly", hosts=("localhost",)).reason == "not_authorized"


# --------------------------------------------------------------------------- redaction and per-target grants

COMPASS = {"id": "staging", "class": "staging", "base_urls": ["https://compass.example.com:8443"]}


def test_a_target_the_ledger_would_redact_is_refused(ws, actor, frozen_now):
    # T10-R11: password_assignment reads "pass…:8443" as a password, so the stored target would cover nothing
    frozen_now.set(NOON)
    cfg = config.load_config(ws)
    cfg["environments"].append(COMPASS)
    config.write_config(ws, cfg)
    before = ledger.iter_all_events(ws)
    for targets, index in ((["compass.example.com:8443"], 0),
                           (["compass.example.com", "https://compass.example.com:8443/"], 1)):
        with pytest.raises(QaError) as e:
            _grant(ws, actor, environment_id="staging", targets=targets, activities=["dast-passive"])
        assert (e.value.code, e.value.details) == ("invalid_target", {"index": index})
    assert ledger.iter_all_events(ws) == before   # the ledger is unchanged
    g = _grant(ws, actor, environment_id="staging", targets=["compass.example.com"], activities=["dast-passive"])
    assert g["data"]["targets"] == ["compass.example.com"] and "redactions" not in g
    assert chk(state.load(ws), cfg, "dast-passive", COMPASS, hosts=("compass.example.com",)).authorization == "AUTH-001"


def test_an_environment_id_the_ledger_would_redact_is_refused(ws, actor, frozen_now):
    # T10-R13: check compares the grant's environment with the id, and config allows any string as one
    frozen_now.set(NOON)
    cfg = config.load_config(ws)
    cfg["environments"].append({"id": "compass:8443", "class": "staging", "base_urls": ["https://qa.example.com"]})
    config.write_config(ws, cfg)
    before = ledger.iter_all_events(ws)
    with pytest.raises(QaError) as e:
        _grant(ws, actor, environment_id="compass:8443", targets=["qa.example.com"])
    assert (e.value.code, e.value.details) == ("invalid_environment", {})
    assert "compass:8443" not in json.dumps(e.value.to_json())
    assert ledger.iter_all_events(ws) == before   # the ledger is unchanged
    renamed = {"id": "compass-staging", "class": "staging", "base_urls": ["https://qa.example.com"]}
    cfg["environments"][-1] = renamed
    config.write_config(ws, cfg)
    _grant(ws, actor, environment_id="compass-staging", targets=["qa.example.com"])
    assert chk(state.load(ws), cfg, "db-readonly", renamed, hosts=("qa.example.com",)).authorization == "AUTH-001"


def test_a_secret_that_equals_an_activity_does_not_void_the_grant(ws, actor, frozen_now, monkeypatch):  # I11
    frozen_now.set(NOON)
    monkeypatch.setenv("QA_PROBE_TOKEN", "db-readonly")
    assert _grant(ws, actor)["data"]["activities"] == ["db-readonly"]


@pytest.mark.parametrize("secret,changes,code,details", [
    ("AUTH-001", {}, "invalid_id", {}),
    ("db-readonly", {}, "invalid_activity", {"index": 0}),
    ("2026-11-07T13:00:00.000Z", {"expires_at": NOON + timedelta(hours=1)}, "invalid_expiry", {}),
])
def test_any_field_check_reads_that_the_ledger_would_redact_is_refused(ws, actor, frozen_now, monkeypatch,
                                                                       secret, changes, code, details):
    # T10-R13: no shipped pattern matches these fields. I11 keeps vocabulary words out of the secrets, so the activity
    # row switches that exclusion off: the refusal stays as the second line of defense.
    frozen_now.set(NOON)
    monkeypatch.setattr(redact, "_closed_vocabulary", frozenset)
    monkeypatch.setenv("QA_PROBE_TOKEN", secret)
    before = ledger.iter_all_events(ws)
    with pytest.raises(QaError) as e:
        _grant(ws, actor, **changes)
    assert (e.value.code, e.value.details) == (code, details)
    assert secret not in json.dumps(e.value.to_json())
    assert ledger.iter_all_events(ws) == before


@pytest.mark.parametrize("activities", [["network-download"], ["dast-passive"], ["dast-active"],
                                        ["db-readonly", "dast-passive"]])
def test_a_per_target_grant_must_list_its_targets(activities):
    assert _refusal(targets=[], activities=activities) == "targets_required"   # T10-R12


def test_other_grants_may_list_no_target():
    assert _build(targets=[], activities=["db-readonly", "load-test"], limits={"load-test": LOAD})["targets"] == []


def test_a_per_target_grant_covers_only_a_check_that_names_its_hosts(ws, actor, frozen_now):
    frozen_now.set(NOON)
    _grant(ws, actor, targets=["localhost"], activities=["dast-passive", "db-readonly"])
    # hand-made, since build_grant refuses a network-download grant that lists no target
    ledger.append(ws, actor, "authorization.granted", _granted("AUTH-002", activities=["network-download"]))
    st, cfg = state.load(ws), config.load_config(ws)
    assert chk(st, cfg, "dast-passive", hosts=("localhost",)).authorization == "AUTH-001"
    assert chk(st, cfg, "dast-passive").reason == "not_authorized"                  # the check names no host
    assert chk(st, cfg, "db-readonly").authorization == "AUTH-001"                  # other activities keep the rule
    assert chk(st, cfg, "network-download").reason == "not_authorized"
    assert chk(st, cfg, "network-download", hosts=("localhost",)).reason == "not_authorized"


def test_check_compares_hostnames_case_insensitively(ws, actor, frozen_now):
    frozen_now.set(NOON)
    _grant(ws, actor, targets=["http://LocalHost:8080/app", "[::1]:9000"], activities=["dast-passive"])
    st, cfg = state.load(ws), config.load_config(ws)
    assert chk(st, cfg, "dast-passive", hosts=("localhost", "::1")).authorization == "AUTH-001"
    assert chk(st, cfg, "dast-passive", hosts=("LOCALHOST",)).authorization == "AUTH-001"
    assert chk(st, cfg, "dast-passive", hosts=("localhost", "127.0.0.1")).reason == "not_authorized"   # every host must be a target


# --------------------------------------------------------------------------- validity and refusals (T10-R3)

def test_a_grant_covers_while_now_is_before_expires_at(ws, actor, frozen_now):
    frozen_now.set(NOON)
    end = NOON + timedelta(hours=1)
    _grant(ws, actor, expires_at=end)
    st, cfg = state.load(ws), config.load_config(ws)

    def decide(now):
        return authz.check(st, cfg, activity="db-readonly", environment=LOCAL, now=now)

    assert decide(end - timedelta(milliseconds=1)).authorization == "AUTH-001"
    assert decide(end).reason == "not_authorized"


def test_unknown_environment_refused():
    assert _refusal(environment_id="staging") == "unknown_environment"


def test_revoke_refusals_and_revoked_at_is_the_event_ts(ws, actor, frozen_now):
    frozen_now.set(NOON)
    _grant(ws, actor)
    with pytest.raises(QaError) as e:
        authz.revoke(ws, actor, "AUTH-002", "typo")
    assert e.value.code == "unknown_authorization"
    frozen_now.advance(60)
    r = authz.revoke(ws, actor, "AUTH-001", "done for today")
    assert (r["type"], r["data"], r["ts"]) == ("authorization.revoked", {"id": "AUTH-001", "reason": "done for today"},
                                               "2026-11-07T12:01:00.000Z")
    held = state.load(ws)["authorizations"]["AUTH-001"]
    assert (held["revoked_at"], held["revoke_reason"]) == (r["ts"], "done for today")
    with pytest.raises(QaError) as e:
        authz.revoke(ws, actor, "AUTH-001", "again")
    assert e.value.code == "already_revoked"


# --------------------------------------------------------------------------- integrity and edge cases (T10-R4)

def test_concurrent_grants_never_share_an_id(ws, actor, monkeypatch, frozen_now):
    frozen_now.set(NOON)   # T10-R1 and T10-R7
    real, took_id, taken = ledger.next_human_id, threading.Event(), []

    def slow_next_id(w, prefix):
        value = real(w, prefix)
        if threading.current_thread().name == "first":
            took_id.set()
            time.sleep(0.3)   # without the lock, the second grant reads the ledger meanwhile
        return value

    monkeypatch.setattr(ledger, "next_human_id", slow_next_id)
    first = threading.Thread(target=lambda: taken.append(_grant(ws, actor)["data"]["id"]), name="first")
    first.start()
    assert took_id.wait(10)
    taken.append(_grant(ws, actor)["data"]["id"])
    first.join(10)
    assert sorted(taken) == ["AUTH-001", "AUTH-002"]


def test_concurrent_revocations_record_one_event(ws, actor, monkeypatch, frozen_now):
    frozen_now.set(NOON)   # T10-R1 and T10-R7
    _grant(ws, actor)
    real, loaded, outcomes = state.load, threading.Event(), []

    def slow_load(w):
        st = real(w)
        if threading.current_thread().name == "first":
            loaded.set()
            time.sleep(0.3)   # without the lock, the second revocation reads the state meanwhile
        return st

    def revoke():
        try:
            outcomes.append(authz.revoke(ws, actor, "AUTH-001", "done")["type"])
        except QaError as exc:
            outcomes.append(exc.code)

    monkeypatch.setattr(state, "load", slow_load)
    first = threading.Thread(target=revoke, name="first")
    first.start()
    assert loaded.wait(10)
    revoke()
    first.join(10)
    assert sorted(outcomes) == ["already_revoked", "authorization.revoked"]
    assert [e["type"] for e in ledger.iter_all_events(ws)].count("authorization.revoked") == 1


def test_a_revocation_of_an_unknown_grant_is_a_violation(ws, actor):
    e = ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-009", "reason": "x"})
    st = state.load(ws)
    assert st["violations"] == [{"event": e["id"], "kind": "unknown_authorization"}] and st["authorizations"] == {}
    assert set(st) == set(state.empty_state())   # T10-R5: no fold-internal key survives the fold


def test_a_duplicate_grant_id_is_a_violation_and_never_revives_a_revoked_grant(ws, actor):
    ledger.append(ws, actor, "authorization.granted", _granted("AUTH-001"))
    ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-001", "reason": "done"})
    dup = ledger.append(ws, actor, "authorization.granted", _granted("AUTH-001", environment="staging"))
    st = state.load(ws)
    held = st["authorizations"]["AUTH-001"]
    assert st["violations"] == [{"event": dup["id"], "kind": "duplicate_id"}]
    assert (held["environment"], held["revoke_reason"]) == ("local", "done")


def test_a_repeated_revocation_keeps_the_first(ws, actor, frozen_now):
    frozen_now.set(NOON)
    ledger.append(ws, actor, "authorization.granted", _granted("AUTH-001"))
    first = ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-001", "reason": "first"})
    frozen_now.advance(60)
    ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-001", "reason": "second"})
    st = state.load(ws)
    held = st["authorizations"]["AUTH-001"]
    assert (held["revoked_at"], held["revoke_reason"], st["violations"]) == (first["ts"], "first", [])


# --------------------------------------------------------------------------- fold order (T10-R5)

def _folds_first_then_stays_revoked(ws, revocation):
    """The revocation folds before its grant, and the folded state still holds the grant revoked."""
    assert [e["type"] for e in ledger.iter_all_events(ws)][-2:] == ["authorization.revoked", "authorization.granted"]
    st = state.load(ws)
    held = st["authorizations"]["AUTH-001"]
    assert (held["revoked_at"], held["revoke_reason"], st["violations"]) == (revocation["ts"], "done", [])
    assert set(st) == set(state.empty_state())   # no fold-internal key survives the fold
    assert chk(st, config.load_config(ws), "db-readonly").reason == "not_authorized"


def test_a_revocation_in_the_same_millisecond_from_a_session_that_sorts_first_still_revokes(ws, actor, frozen_now):
    frozen_now.set(NOON)
    _grant(ws, dataclasses.replace(actor, session="S-" + "1" * 26))
    _folds_first_then_stays_revoked(ws, authz.revoke(ws, actor, "AUTH-001", "done"))   # actor's S-000… sorts first


def test_a_revocation_recorded_after_the_clock_stepped_back_still_revokes(ws, actor, frozen_now):
    frozen_now.set(NOON + timedelta(seconds=1))
    _grant(ws, actor)
    frozen_now.set(NOON)   # the clock steps back 1 s within one session
    _folds_first_then_stays_revoked(ws, authz.revoke(ws, actor, "AUTH-001", "done"))


def test_the_first_revocation_in_fold_order_wins_even_before_the_grant(ws, actor, frozen_now):
    frozen_now.set(NOON)
    ledger.append(ws, actor, "authorization.granted", _granted("AUTH-001"))
    frozen_now.set(NOON - timedelta(seconds=2))
    first = ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-001", "reason": "first"})
    frozen_now.set(NOON - timedelta(seconds=1))
    ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-001", "reason": "second"})
    frozen_now.set(NOON + timedelta(seconds=1))
    ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-001", "reason": "third"})
    st = state.load(ws)
    held = st["authorizations"]["AUTH-001"]
    assert (held["revoked_at"], held["revoke_reason"], st["violations"]) == (first["ts"], "first", [])


def test_only_a_revocation_whose_id_no_grant_holds_is_unknown(ws, actor, frozen_now):
    frozen_now.set(NOON)
    ledger.append(ws, actor, "authorization.granted", _granted("AUTH-001"))
    frozen_now.set(NOON - timedelta(seconds=1))
    ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-001", "reason": "early"})
    unknown = ledger.append(ws, actor, "authorization.revoked", {"id": "AUTH-009", "reason": "typo"})
    st = state.load(ws)
    assert st["violations"] == [{"event": unknown["id"], "kind": "unknown_authorization"}]
    assert list(st["authorizations"]) == ["AUTH-001"] and st["authorizations"]["AUTH-001"]["revoke_reason"] == "early"
    assert set(st) == set(state.empty_state())


@pytest.mark.parametrize("activity,limits", [("db-write", {"db-write": {"backup_statement": "snapshot taken at 11:50"}}),
                                             ("dast-active", {})])
def test_never_on_production_cannot_be_granted(activity, limits):
    prod = dict(environment_id="prod", targets=["https://shop.example"], activities=[activity], limits=limits)
    assert _refusal(_cfg(PROD), **prod) == "invalid_activity"
    staging = dict(environment_id="staging", targets=["https://staging.shop.example"], activities=[activity], limits=limits)
    assert _build(_cfg(STAGING), **staging)["activities"] == [activity]


# --------------------------------------------------------------------------- the brief's other rules

@pytest.mark.parametrize("activities", [["made-up"], [], ["db-readonly", "credential-use"]])
def test_a_grant_needs_known_activities_the_suite_performs(activities):
    assert _refusal(activities=activities) == "invalid_activity"


@pytest.mark.parametrize("env_id,activity,limits", [
    ("local", "load-test", {"load-test": {**LOAD, "max_vus": 0}}),
    ("local", "load-test", {"load-test": {**LOAD, "max_rps": True}}),
    ("local", "load-test", {"load-test": {**LOAD, "max_duration_s": "600"}}),
    ("prod", "load-test", {"load-test": LOAD}),                                       # production also needs coordination
    ("local", "db-write", {}),
    ("local", "db-write", {"db-write": {"backup_statement": " "}}),
    ("local", "write-tests", {"write-tests": {"paths": []}}),
    ("local", "write-tests", {"write-tests": {"paths": "tests/**"}}),               # a list of globs, not one string
    ("local", "data-egress", {"data-egress": {"service": "api.osv.dev"}}),
    ("local", "data-egress", {"data-egress": {"data": "dependency names and versions"}}),
    ("local", "db-readonly", {"db-write": {"backup_statement": "nightly dump"}}),   # keyed by an activity the grant lacks
    ("local", "db-readonly", {"db-readonly": "read only"}),                         # each activity's limits are an object
    ("local", "db-readonly", ["read only"]),                                        # limits are an object
])
def test_limit_rules(env_id, activity, limits):
    targets = {"local": ["localhost"], "prod": ["https://shop.example"]}[env_id]
    assert _refusal(_cfg(PROD), environment_id=env_id, targets=targets, activities=[activity], limits=limits) == "invalid_limits"


@pytest.mark.parametrize("path", ["/abs/**", "~/tests/**", "../x/**", "tests/../../x", ".", ".claude/**",
                                  "**/CLAUDE.md", ".vscode/settings.json", "**"])
def test_write_tests_paths_follow_the_write_scope_rules(path):
    # T10-R9: absolute, '..', selects nothing, or §12.4's protected agent and IDE configuration
    limits = {"write-tests": {"paths": ["tests/**", path]}}
    assert _refusal(activities=["write-tests"], limits=limits) == "invalid_limits"


def test_limits_that_meet_the_rules_are_stored_as_given():
    prod = {"load-test": {**LOAD, "coordination": "ops on call, window 02:00-03:00 UTC"}}
    g = _build(_cfg(PROD), environment_id="prod", targets=["https://shop.example"], activities=["load-test"], limits=prod)
    assert g["limits"] == prod
    local = {"load-test": {**LOAD, "ramp_up_s": 30}, "db-write": {"backup_statement": "snapshot taken at 11:50"},
             "write-tests": {"paths": ["tests/**", "spec/**"]},
             "data-egress": {"service": "api.osv.dev", "data": "dependency names and versions"}}
    assert _build(activities=list(local), limits=local)["limits"] == local


def test_build_grant_refusal_order():
    cfg = _cfg({"id": "ci", "class": "ci"})
    assert _refusal(cfg, approval=" ", environment_id="nope") == "approval_required"
    assert _refusal(cfg, environment_id="nope", activities=["deploy"]) == "unknown_environment"
    assert _refusal(cfg, environment_id="ci", activities=["deploy"]) == "ci_unsupported"
    assert _refusal(cfg, activities=["deploy"], targets=["db.example.com"]) == "invalid_activity"
    assert _refusal(cfg, activities=["deploy"], targets=[""]) == "invalid_activity"
    assert _refusal(cfg, activities=["deploy", "dast-passive"], targets=[]) == "invalid_activity"
    assert _refusal(cfg, activities=["dast-passive", "load-test"], targets=[]) == "targets_required"
    # every target must name a host before any is resolved
    assert _refusal(cfg, activities=["load-test"], targets=["db.example.com", ""]) == "invalid_target"
    assert _refusal(cfg, activities=["load-test"], targets=["db.example.com"]) == "undeclared_target"
    assert _refusal(cfg, activities=["load-test"], expires_at=NOON) == "invalid_limits"


def test_standing_authorization_covers_only_its_environment_and_activities():
    cfg = _cfg()
    cfg["standing_authorizations"] = [{"environment": "local", "activities": ["install-dependencies"]}]
    d = chk(state.empty_state(), cfg, "install-dependencies")
    assert (d.allowed, d.reason, d.authorization) == (True, None, "standing:local")
    d = chk(state.empty_state(), cfg, "install-tools")
    assert (d.allowed, d.reason, d.authorization) == (False, "not_authorized", None)
    assert chk(state.empty_state(), cfg, "install-dependencies", EPH).reason == "not_authorized"


@pytest.mark.parametrize("trust,env,expected", [
    ("untrusted", EPH, ("not_authorized", None)),       # a standing authorization covers eph
    ("untrusted", STAGING, ("not_authorized", None)),   # a grant covers staging
    ("untrusted", CI, ("policy_denied", None)),         # steps (1)-(4) come first
    ("trusted", EPH, (None, "standing:eph")),
    ("trusted", STAGING, (None, "AUTH-001")),
    ("trusted", PROD, ("not_authorized", None)),        # nothing covers prod
])
def test_trust_decides_local_test_runs_on_every_class(ws, actor, frozen_now, trust, env, expected):
    # T10-R8: the tests run on this machine whatever the run environment's class
    frozen_now.set(NOON)
    ledger.append(ws, actor, "authorization.granted",
                  _granted("AUTH-001", environment="staging", activities=["test-execution-local"]))
    cfg = _cfg(EPH, STAGING, PROD, CI)
    cfg["project"]["trust"] = trust
    cfg["standing_authorizations"] = [{"environment": "eph", "activities": ["test-execution-local"]}]
    d = chk(state.load(ws), cfg, "test-execution-local", env)
    assert (d.reason, d.authorization) == expected
    assert (d.detail == UNTRUSTED) == (trust == "untrusted" and env is not CI)


def test_a_grant_covers_only_its_environment_and_activities(ws, actor, frozen_now):
    frozen_now.set(NOON)
    _grant(ws, actor)
    st, cfg = state.load(ws), config.load_config(ws)
    assert chk(st, cfg, "db-readonly").authorization == "AUTH-001"
    assert chk(st, cfg, "db-ephemeral").reason == "not_authorized"
    assert chk(st, cfg, "db-readonly", STAGING).reason == "not_authorized"


def test_the_lowest_valid_grant_is_reported(ws, actor, frozen_now):
    frozen_now.set(NOON)
    _grant(ws, actor)
    _grant(ws, actor)
    assert chk(state.load(ws), config.load_config(ws), "db-readonly").authorization == "AUTH-001"
    authz.revoke(ws, actor, "AUTH-001", "superseded")
    assert chk(state.load(ws), config.load_config(ws), "db-readonly").authorization == "AUTH-002"


def test_a_stored_grant_whose_expiry_names_no_real_time_covers_nothing(ws, actor, frozen_now):
    frozen_now.set(NOON)
    ledger.append(ws, actor, "authorization.granted", _granted("AUTH-001"))
    ledger.append(ws, actor, "authorization.granted",
                  _granted("AUTH-002", activities=["db-ephemeral"], expires_at="2026-02-30T00:00:00.000Z"))
    st, cfg = state.load(ws), config.load_config(ws)
    assert chk(st, cfg, "db-readonly").authorization == "AUTH-001"
    assert chk(st, cfg, "db-ephemeral").reason == "not_authorized"


def test_redacted_text_still_folds_and_the_grant_still_works(ws, actor, frozen_now):
    # T10-R6 refuses URL targets with userinfo, so the redacted text sits in the approval quote and a limit.
    frozen_now.set(NOON)
    g = _grant(ws, actor, targets=["http://localhost:8080/"], activities=["db-write"],
               limits={"db-write": {"backup_statement": "pg_dump run with pwd=hunter22x"}},
               approval="yes; the password=hunter22x login is a test account")
    assert "hunter22x" not in json.dumps(g["data"]) and g["redactions"] >= 2
    st = state.load(ws)
    assert st["violations"] == [] and st["authorizations"]["AUTH-001"]["limits"] == g["data"]["limits"]
    assert st["authorizations"]["AUTH-001"]["approval_quote"] == g["data"]["approval_quote"]
    assert chk(st, config.load_config(ws), "db-write", hosts=("localhost",)).authorization == "AUTH-001"
