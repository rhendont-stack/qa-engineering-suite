from __future__ import annotations

import copy

import pytest

import qa
from qa import canonical, config, ledger, schema, state, vocab, workspace
from qa.errors import QaError


@pytest.fixture
def fresh(ws, actor):
    config.record_initialized(ws, actor)
    return ws


def _append_space(w):
    w.path("config.json").write_bytes(w.path("config.json").read_bytes() + b" ")


def test_initialized_config_is_accepted(fresh, actor):
    assert config.check_drift(fresh, state.load(fresh), actor, "test-execution-local") is None


def test_manual_edit_is_drift_recorded_once(fresh, actor):
    fresh.path("config.json").write_bytes(fresh.path("config.json").read_bytes() + b" ")
    for _ in range(2):
        assert config.check_drift(fresh, state.load(fresh), actor, "test-execution-local") == "config_drift"
    assert [e["type"] for e in ledger.iter_all_events(fresh)].count("config.drift_detected") == 1


def test_static_analysis_exempt(fresh, actor):
    fresh.path("config.json").write_bytes(fresh.path("config.json").read_bytes() + b" ")
    assert config.check_drift(fresh, state.load(fresh), actor, "static-analysis") is None


def test_accept_clears_drift(fresh, actor):
    fresh.path("config.json").write_bytes(fresh.path("config.json").read_bytes() + b" ")
    config.accept(fresh, actor, "I reviewed the edit")
    assert config.check_drift(fresh, state.load(fresh), actor, "test-execution-local") is None


def test_set_value_records_change(fresh, actor):
    e = config.set_value(fresh, actor, "project.trust", "trusted", "Mark the suite repo trusted")
    assert (e["data"]["old"], e["data"]["new"]) == ("untrusted", "trusted")
    assert config.check_drift(fresh, state.load(fresh), actor, "test-execution-local") is None


def test_current_and_accepted_hash(fresh):
    h = config.current_hash(fresh)
    assert h == canonical.sha256_bytes(fresh.path("config.json").read_bytes()) == config.accepted_hash(state.load(fresh))
    assert config.accepted_hash(state.empty_state()) is None


def test_write_config_validates_and_returns_hash(fresh):
    cfg = config.load_config(fresh)
    cfg["defaults"]["timeout_seconds"] = 60
    h = config.write_config(fresh, cfg)
    assert h == config.current_hash(fresh) and fresh.path("config.json").read_bytes() == workspace.config_bytes(cfg)
    cfg["defaults"]["timeout_seconds"] = 0
    with pytest.raises(QaError) as e:
        config.write_config(fresh, cfg)
    assert e.value.code == "config_invalid" and config.current_hash(fresh) == h


@pytest.mark.parametrize("key,value,approval,code", [
    ("project.trust", "trusted", "", "approval_required"),
    ("gate.overrides.mutation_score_min", {"enabled": True}, "ok", "use_gate_policy_accept"),
    ("defaults.timeout_seconds", 0, "ok", "config_invalid"),
])
def test_set_value_refusals(fresh, actor, key, value, approval, code):
    with pytest.raises(QaError) as e:
        config.set_value(fresh, actor, key, value, approval)
    assert e.value.code == code


@pytest.mark.parametrize("mutate", [
    lambda c: c["environments"].append(dict(c["environments"][0])),
    lambda c: c["environments"].append({"id": "eph", "class": "ephemeral", "base_urls": []}),
    lambda c: c["environments"].append({"id": "eph", "class": "ephemeral", "base_urls": [], "ephemeral_basis": "suite-created"}),
    lambda c: c.update(standing_authorizations=[{"environment": "local", "activities": ["deploy"]}]),
    lambda c: c.update(write_scopes=["../outside/**"]),
    lambda c: c.update(write_scopes=[".claude/**"]),
    lambda c: c["defaults"].update(max_log_mb=2000),
    lambda c: c["defaults"].update(flaky_reruns=11),
    lambda c: c["gate"].update(profile="yolo"),
    lambda c: c["gate"]["overrides"].update(mutation_score_min={"enabled": True}),
])
def test_semantic_errors(mutate):
    c = workspace.default_config("x")
    mutate(c)
    assert config.semantic_errors(c)


def test_cloned_config_is_drift(tmp_path, actor):
    root = tmp_path / "clone"
    (root / ".qa").mkdir(parents=True)
    (root / ".qa" / "config.json").write_bytes(workspace.config_bytes(workspace.default_config("clone")))
    w, created = workspace.init_workspace(root)
    assert not created and config.check_drift(w, state.load(w), actor, "test-execution-local") == "config_drift"


def test_vocab_matches_defs():
    defs = schema.load_schema("defs.schema.json")["$defs"]
    assert set(vocab.ACTIVITIES) == set(defs["activity"]["enum"]) and set(vocab.ENV_CLASSES) == set(defs["env_class"]["enum"])


def test_resolve_host_ignores_case_and_port():
    assert config.resolve_host(workspace.default_config("x"), "LOCALHOST:8080")["id"] == "local"
    assert config.resolve_host(workspace.default_config("x"), "example.com") is None


# ---- Rulings beyond the brief's tests ----

def test_vocab_values():
    assert vocab.ACTIVITIES == tuple(schema.load_schema("defs.schema.json")["$defs"]["activity"]["enum"])
    assert vocab.ENV_CLASSES == tuple(schema.load_schema("defs.schema.json")["$defs"]["env_class"]["enum"])
    assert vocab.NEVER_ACTIVITIES == ("credential-use", "deploy")
    assert set(vocab.RUNNER_CONFINED) == {"static-analysis", "test-execution-local", "install-dependencies",
                                          "network-download", "db-ephemeral"}
    assert vocab.PROFILES == ("change", "fix", "audit", "release")
    assert ".github/copilot-instructions.md" in vocab.PROTECTED_PATHS and len(vocab.PROTECTED_PATHS) == 9


def _code_errors(fn, *args):
    with pytest.raises(QaError) as e:
        fn(*args)
    return e.value


def test_load_config_ok(fresh):
    assert config.load_config(fresh)["project"]["name"] == fresh.root.name


def test_load_config_unparseable_and_non_object(fresh):
    fresh.path("config.json").write_bytes(b"{nope")
    e = _code_errors(config.load_config, fresh)
    assert e.code == "config_invalid" and e.details["errors"]
    fresh.path("config.json").write_bytes(b"[1]")
    e = _code_errors(config.load_config, fresh)
    assert e.code == "config_invalid" and e.details["errors"]


def test_load_config_schema_failure_reports_schema_messages(fresh):
    cfg = workspace.default_config("x")
    cfg["extra"] = 1
    cfg["defaults"]["timeout_seconds"] = 0  # semantic problem too; schema messages win
    fresh.path("config.json").write_bytes(workspace.config_bytes(cfg))
    e = _code_errors(config.load_config, fresh)
    assert e.code == "config_invalid"
    assert e.details["errors"] == schema.errors(cfg, schema.load_schema("config.schema.json"), base="config.schema.json")


def test_load_config_semantic_failure(fresh):
    cfg = workspace.default_config("x")
    cfg["defaults"]["timeout_seconds"] = 0
    fresh.path("config.json").write_bytes(workspace.config_bytes(cfg))
    e = _code_errors(config.load_config, fresh)
    assert e.code == "config_invalid" and e.details["errors"] == config.semantic_errors(cfg)


def test_write_config_rejects_schema_invalid(fresh):
    before = fresh.path("config.json").read_bytes()
    cfg = workspace.default_config("x")
    cfg["bogus"] = True
    assert _code_errors(config.write_config, fresh, cfg).code == "config_invalid"
    assert fresh.path("config.json").read_bytes() == before


def _with(**kw):
    c = workspace.default_config("x")
    c.update(kw)
    return c


def test_semantic_standing_authorization_rules():
    ok = _with(standing_authorizations=[{"environment": "local", "activities": ["test-execution-local"]}])
    assert config.semantic_errors(ok) == []
    unknown = _with(standing_authorizations=[{"environment": "nope", "activities": ["static-analysis"]}])
    assert config.semantic_errors(unknown)
    staging = _with(environments=[{"id": "stg", "class": "staging", "base_urls": []}],
                    standing_authorizations=[{"environment": "stg", "activities": ["static-analysis"]}])
    assert config.semantic_errors(staging)
    ci = _with(environments=[{"id": "ci", "class": "ci", "base_urls": []}],
               standing_authorizations=[{"environment": "ci", "activities": ["static-analysis", "network-download"]}])
    assert config.semantic_errors(ci) == []
    ci_bad = _with(environments=[{"id": "ci", "class": "ci", "base_urls": []}],
                   standing_authorizations=[{"environment": "ci", "activities": ["write-tests"]}])
    assert config.semantic_errors(ci_bad)
    eph = _with(environments=[{"id": "e", "class": "ephemeral", "base_urls": [], "ephemeral_basis": "user-declared"}],
                standing_authorizations=[{"environment": "e", "activities": ["db-write", "credential-use"]}])
    assert config.semantic_errors(eph)  # never-activity in any class


@pytest.mark.parametrize("scope", [
    "/abs/x", "\\abs", "~/x", "C:/x", "c:\\x", "a/../b", "..", "a\\..\\b",
    "**", "*.md", ".github/**", ".claude/*.json", "./.claude/x", "././.vscode/settings.json",
    "CLAUDE.md", "claude.MD", "agents.md", ".GitHub/Copilot-Instructions.md", ".github/agents/x.md",
    ".mcp.json", "mcp.json", ".cursor/rules", ".claude", "*",
    "**/CLAUDE.md", "**/.claude/**", "**/.vscode/*.json", "**/**/agents.md",
    ".github/./agents/x.md", ".github/./copilot-instructions.md", ".github//agents/x.md",
    ".", "./", "", "./.", "//",
])
def test_semantic_refuses_write_scope(scope):
    assert config.semantic_errors(_with(write_scopes=[scope]))


@pytest.mark.parametrize("scope", ["tests/**", "./tests/unit/*.py", "**/tests/**", "tests/./unit/*.py", "src/app/test_*.py", ".github/workflows/*.yml"])
def test_semantic_allows_write_scope(scope):
    assert config.semantic_errors(_with(write_scopes=[scope])) == []


def test_check_drift_static_analysis_reads_nothing(fresh, actor):
    fresh.path("config.json").write_bytes(b"{broken")
    assert config.check_drift(fresh, state.empty_state(), actor, "static-analysis") is None
    assert "config.drift_detected" not in [e["type"] for e in ledger.iter_all_events(fresh)]


def test_check_drift_unparseable_or_schema_invalid_raises(fresh, actor):
    fresh.path("config.json").write_bytes(b"{broken")
    assert _code_errors(config.check_drift, fresh, state.load(fresh), actor, "test-execution-local").code == "config_invalid"
    cfg = workspace.default_config("x")
    cfg["extra"] = 1
    fresh.path("config.json").write_bytes(workspace.config_bytes(cfg))
    assert _code_errors(config.check_drift, fresh, state.load(fresh), actor, "test-execution-local").code == "config_invalid"


def test_check_drift_records_semantically_invalid_but_schema_valid(fresh, actor):
    cfg = config.load_config(fresh)
    cfg["write_scopes"] = [".claude/**"]
    fresh.path("config.json").write_bytes(workspace.config_bytes(cfg))
    assert config.check_drift(fresh, state.load(fresh), actor, "write-tests") == "config_drift"
    assert [e["type"] for e in ledger.iter_all_events(fresh)].count("config.drift_detected") == 1


def test_check_drift_distinct_hashes_each_recorded_with_accepted_hash(fresh, actor):
    accepted = config.accepted_hash(state.load(fresh))
    _append_space(fresh)
    config.check_drift(fresh, state.load(fresh), actor, "write-tests")
    _append_space(fresh)
    config.check_drift(fresh, state.load(fresh), actor, "write-tests")
    drift = state.load(fresh)["config"]["drift"]
    assert len(drift) == 2 and drift[0]["hash"] != drift[1]["hash"]
    assert all(d["accepted_hash"] == accepted for d in drift)
    assert set(drift[0]) == {"event", "hash", "accepted_hash"}


def test_check_drift_never_accepted_has_null_accepted_hash(ws, actor):
    assert config.check_drift(ws, state.load(ws), actor, "write-tests") == "config_drift"
    assert state.load(ws)["config"]["drift"][0]["accepted_hash"] is None


def test_check_drift_rechecks_state_inside_lock(fresh, actor):
    _append_space(fresh)
    stale = state.load(fresh)
    assert config.check_drift(fresh, stale, actor, "write-tests") == "config_drift"
    assert config.check_drift(fresh, stale, actor, "write-tests") == "config_drift"  # stale st, still once
    assert [e["type"] for e in ledger.iter_all_events(fresh)].count("config.drift_detected") == 1


def test_set_value_refusal_order(fresh, actor):
    # gate key beats an empty approval
    assert _code_errors(config.set_value, fresh, actor, "gate", {}, "").code == "use_gate_policy_accept"
    assert _code_errors(config.set_value, fresh, actor, "gate.profile", "fix", " ").code == "use_gate_policy_accept"
    # empty-after-strip approval beats drift
    _append_space(fresh)
    assert _code_errors(config.set_value, fresh, actor, "project.trust", "trusted", "  \n").code == "approval_required"
    # drift
    assert _code_errors(config.set_value, fresh, actor, "project.trust", "trusted", "ok").code == "config_drift"


def test_set_value_key_prefix_gate_only_for_gate_segment(fresh, actor):
    # "gateway" is not a gate key; it fails as an unknown member instead
    assert _code_errors(config.set_value, fresh, actor, "gateway", 1, "ok").code == "config_invalid"


def test_set_value_parses_strings_as_json(fresh, actor):
    e = config.set_value(fresh, actor, "defaults.timeout_seconds", "120", "ok")
    assert e["data"]["new"] == 120 and config.load_config(fresh)["defaults"]["timeout_seconds"] == 120
    e = config.set_value(fresh, actor, "write_scopes", '["tests/**"]', "ok")
    assert e["data"]["new"] == ["tests/**"]
    e = config.set_value(fresh, actor, "project.name", "plain words", "ok")
    assert e["data"]["new"] == "plain words"
    e = config.set_value(fresh, actor, "defaults.flaky_reruns", 1, "ok")
    assert e["data"]["new"] == 1 and e["type"] == "config.changed"


def test_set_value_key_path_errors(fresh, actor):
    before = fresh.path("config.json").read_bytes()
    for key in ("nope.child", "environments.id", "project.trust.x", "", "project..name", "project.name."):
        assert _code_errors(config.set_value, fresh, actor, key, 1, "ok").code == "config_invalid", key
    assert _code_errors(config.set_value, fresh, actor, "project.extra", 1, "ok").code == "config_invalid"
    assert fresh.path("config.json").read_bytes() == before


def test_set_value_old_is_current_value(fresh, actor):
    e = config.set_value(fresh, actor, "redaction.extra_env_names", '["MY_TOKEN"]', "ok")
    assert e["data"]["old"] == []
    assert e["data"]["hash"] == config.current_hash(fresh)


def test_set_value_restores_file_when_record_fails(fresh, actor, monkeypatch):
    before = fresh.path("config.json").read_bytes()

    def boom(*a, **k):
        raise QaError("ledger_write_failed", "x")

    monkeypatch.setattr(ledger, "append", boom)
    assert _code_errors(config.set_value, fresh, actor, "project.trust", "trusted", "ok").code == "ledger_write_failed"
    assert fresh.path("config.json").read_bytes() == before


def test_accept_refusals_and_envelope(fresh, actor):
    assert _code_errors(config.accept, fresh, actor, " ").code == "approval_required"
    fresh.path("config.json").write_bytes(b"{broken")
    assert _code_errors(config.accept, fresh, actor, "ok").code == "config_invalid"
    cfg = workspace.default_config("x")
    cfg["defaults"]["max_log_mb"] = 2000
    fresh.path("config.json").write_bytes(workspace.config_bytes(cfg))
    assert _code_errors(config.accept, fresh, actor, "ok").code == "config_invalid"
    fresh.path("config.json").write_bytes(workspace.config_bytes(workspace.default_config("y")))
    e = config.accept(fresh, actor, "looks right")
    assert e["type"] == "config.accepted"
    assert e["data"] == {"hash": config.current_hash(fresh), "approval": "looks right"}


def test_event_schemas():
    h = "sha256:" + "a" * 64
    ok = {"key": "a.b", "old": None, "new": {"x": [1]}, "hash": h, "approval": "ok"}
    schema.validate(ok, "events/config.changed.schema.json")
    for bad in (dict(ok, key=""), dict(ok, approval=""), dict(ok, hash="x"), dict(ok, extra=1),
                {k: v for k, v in ok.items() if k != "old"}):
        with pytest.raises(QaError):
            schema.validate(bad, "events/config.changed.schema.json")
    schema.validate({"hash": h, "approval": "ok"}, "events/config.accepted.schema.json")
    with pytest.raises(QaError):
        schema.validate({"hash": h, "approval": ""}, "events/config.accepted.schema.json")
    schema.validate({"hash": h, "accepted_hash": None}, "events/config.drift_detected.schema.json")
    schema.validate({"hash": h, "accepted_hash": h}, "events/config.drift_detected.schema.json")
    with pytest.raises(QaError):
        schema.validate({"hash": h}, "events/config.drift_detected.schema.json")


def test_reducers_set_hash_and_history(fresh, actor):
    changed = config.set_value(fresh, actor, "project.trust", "trusted", "ok")
    _append_space(fresh)
    accepted = config.accept(fresh, actor, "fine")
    cfg = state.load(fresh)["config"]
    assert cfg["accepted_hash"] == accepted["data"]["hash"]
    assert [(h["kind"]) for h in cfg["history"]] == ["initialized", "changed", "accepted"]
    assert cfg["history"][1] == {"event": changed["id"], "kind": "changed", "hash": changed["data"]["hash"]}
    assert cfg["history"][2] == {"event": accepted["id"], "kind": "accepted", "hash": accepted["data"]["hash"]}


def test_record_initialized(ws, actor):
    e = config.record_initialized(ws, actor)
    assert e["type"] == "workspace.initialized"
    assert e["data"] == {"kernel_version": qa.KERNEL_VERSION, "schema_version": qa.SCHEMA_VERSION,
                         "config_hash": config.current_hash(ws)}


def test_resolve_host_variants():
    cfg = workspace.default_config("x")
    cfg["environments"].append({"id": "stg", "class": "staging", "base_urls": ["https://Staging.Example.com:8443/app"]})
    cfg["environments"].append({"id": "dup", "class": "staging", "base_urls": ["http://localhost"]})
    for host in ("localhost", "LocalHost:3000", "127.0.0.1", "127.0.0.1:80", "[::1]", "[::1]:8080", "::1"):
        assert config.resolve_host(cfg, host)["id"] == "local", host
    assert config.resolve_host(cfg, "staging.example.com:1")["id"] == "stg"
    assert config.resolve_host(cfg, "other") is None
