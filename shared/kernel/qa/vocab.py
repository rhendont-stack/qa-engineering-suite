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
