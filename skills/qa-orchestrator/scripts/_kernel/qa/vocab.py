from __future__ import annotations

# Shared vocabulary (§12.2, §12.4). Later modules import these; none redefines them.
# tests/unit/test_config.py checks ACTIVITIES and ENV_CLASSES against defs.schema.json.

ACTIVITIES = (
    "static-analysis", "test-execution-local", "write-tests",
    "install-tools", "install-dependencies", "network-download", "data-egress",
    "credential-use", "db-ephemeral", "db-readonly", "db-write",
    "dast-passive", "dast-active", "load-test", "device-control",
    "git-history-scan", "deploy",
)
NEVER_ACTIVITIES = ("credential-use", "deploy")
# §12.2: network-download "Requires a grant listing hosts" and DAST "Requires a grant per target".
PER_TARGET_ACTIVITIES = ("network-download", "dast-passive", "dast-active")
# Activities a `ci` class may use: those confined to the runner (§12.2 Unattended CI).
RUNNER_CONFINED = ("static-analysis", "test-execution-local", "install-dependencies",
                   "network-download", "db-ephemeral")
ENV_CLASSES = ("local", "ci", "ephemeral", "shared-test", "staging", "production")
PROFILES = ("change", "fix", "audit", "release")
# Agent and IDE configuration the suite never writes (§12.4). A trailing "/" marks a directory.
PROTECTED_PATHS = (
    ".claude/", "CLAUDE.md", "AGENTS.md", ".cursor/", ".vscode/",
    ".github/copilot-instructions.md", ".github/agents/", ".mcp.json", "mcp.json",
)

# Finding lifecycle (§10.6): {current state: {next state: the fields the transition needs}}. Every transition needs a
# reason. TRANSITIONS holds only what a person may do by hand. fix_submitted, verified_fixed and reopened are set by fixes and
# revalidation alone (KERNEL_STATES; FIX_TRANSITIONS says from where), so none is a target here.
TRANSITIONS = {
    "open": {"accepted_risk": ("reason", "accepted_by", "expires_at"), "wont_fix": ("reason",),
             "duplicate": ("reason", "duplicate_of")},
    "accepted_risk": {"open": ("reason",)},
    "wont_fix": {"open": ("reason",)},
    "duplicate": {"open": ("reason",)},
    # A finding whose fix cannot be revalidated, or that stays broken, can still be set aside by a person.
    "fix_submitted": {"accepted_risk": ("reason", "accepted_by", "expires_at"), "wont_fix": ("reason",)},
    "reopened": {"accepted_risk": ("reason", "accepted_by", "expires_at"), "wont_fix": ("reason",)},
}
KERNEL_STATES = ("fix_submitted", "verified_fixed", "reopened")
# {state: the states a fix or a revalidation moves a finding to}; a newer fix supersedes a fix_submitted one.
FIX_TRANSITIONS = {"open": ("fix_submitted",), "reopened": ("fix_submitted",), "fix_submitted": ("fix_submitted", "verified_fixed", "reopened")}
