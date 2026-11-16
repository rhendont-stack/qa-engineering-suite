"""Checks the plugin marketplace: ``.claude-plugin/marketplace.json``, its four entries and the guard plugin's manifest.

Usage: python3 -I tools/check_packaging.py [root]      (root defaults to the current directory)

The marketplace lists, in order: ``qa-engineering-suite`` (every skill and agent through the default scans), the bundles
``qa-core`` and ``qa-specialists`` (``strict: false`` entries that name their skills; they partition ``catalog.json`` and
never overlap), and ``qa-command-guard`` (a separate plugin under ``plugins/``). The guard plugin holds its manifest, one
PreToolUse command hook on ``Bash`` (``hooks/hooks.json``), ``scripts/guard_hook.py``, the vendored ``scripts/_kernel/`` with
its manifest, and a ``README.md``. Every entry and the guard's ``plugin.json`` share one version: the skills' common ``metadata.suite-version``, optionally followed by ``-<pre-release>``. The repository
root holds no ``.claude-plugin/plugin.json``, ``hooks/hooks.json``, ``bin/`` or ``CLAUDE.md``, and no name is reserved.

Prints one line per problem, ``<path>: <kind>: <detail>``, sorted, then a count. Exit 0 ok, 1 problems, 2 usage.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# ``-I`` ignores PYTHONPATH: use the kernel of the repository this script belongs to, whichever root it checks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "kernel"))

from qa import catalog  # noqa: E402

CORE = ("qa-orchestrator", "qa-requirements", "qa-codebase-intelligence", "qa-test-strategy", "qa-test-generation",
        "qa-test-execution", "qa-regression-guard", "qa-reporting")
SPECIALISTS = ("qa-api-testing", "qa-database", "qa-security", "qa-performance", "qa-frontend", "qa-mobile", "qa-mutation",
               "qa-release")
MARKETPLACE = ".claude-plugin/marketplace.json"
GUARD_PATH = "plugins/qa-command-guard"
GUARD_MANIFEST = GUARD_PATH + "/.claude-plugin/plugin.json"
GUARD_HOOK_COMMAND = 'python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/guard_hook.py"'
GUARD_FILES = ("hooks/hooks.json", "scripts/guard_hook.py", "scripts/_kernel/MANIFEST.sha256", "README.md")
ENTRIES = ("qa-engineering-suite", "qa-core", "qa-specialists", "qa-command-guard")
BUNDLES = {"qa-core": CORE, "qa-specialists": SPECIALISTS}
OWNER = {"name": "rhendont-stack", "url": "https://github.com/rhendont-stack"}
HOMEPAGE = "https://github.com/rhendont-stack/qa-engineering-suite"
LICENSE = "Apache-2.0"
FORBIDDEN_ROOT = (".claude-plugin/plugin.json", "hooks/hooks.json", "bin", "CLAUDE.md")
RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-", "claudeai-")
RESERVED_NAMES = {
    "claude", "anthropic", "anthropics", "claude-code", "claude-mods", "claude-code-marketplace", "claude-code-plugins",
    "claude-plugins-official", "anthropic-marketplace", "anthropic-plugins", "agent-skills", "anthropic-agent-skills",
    "life-sciences", "knowledge-work-plugins", "claude-for-legal", "claude-for-financial-services",
    "financial-services-plugins", "first-party-plugins", "claude-tag-plugins", "claude-community",
    "claude-plugins-community", "healthcare", "anthropic-plugin-directory", "claude-plugin-directory", "inline",
    "builtin", "skills-dir", "synced", "claude-plugin-test"}
COMPONENT_FIELDS = ("skills", "agents", "commands", "hooks", "outputStyles", "themes", "strict")
Problem = str


def problem(path: str, kind: str, detail: str) -> Problem:
    return "%s: %s: %s" % (path, kind, detail)


def reserved(name: object) -> bool:
    return not isinstance(name, str) or name in RESERVED_NAMES or name.startswith(RESERVED_PREFIXES)


def load(root: Path, relative: str, out: List[Problem]) -> Optional[dict]:
    try:
        doc = json.loads((root / relative).read_text(encoding="utf-8"))
    except FileNotFoundError:
        out.append(problem(relative, "missing_manifest", "the file does not exist"))
        return None
    except (ValueError, UnicodeDecodeError):
        out.append(problem(relative, "bad_json", "not valid JSON"))
        return None
    if not isinstance(doc, dict):
        out.append(problem(relative, "bad_json", "expected a JSON object"))
        return None
    return doc


def suite_versions(root: Path) -> List[str]:
    found = set()
    for skill in sorted((root / "skills").glob("*/SKILL.md")):
        front = catalog.parse_frontmatter(skill.read_text(encoding="utf-8", errors="replace")) or {}
        meta = front.get("metadata")
        found.add(meta.get("suite-version", "") if isinstance(meta, dict) else "")
    return sorted(found)


def listed_skills(root: Path) -> List[str]:
    try:
        doc = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
        return [n for n in doc["skills"] if isinstance(n, str)]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def check_metadata(where: str, item: dict, out: List[Problem]) -> None:
    if item.get("author") != OWNER:
        out.append(problem(where, "bad_metadata", "author must be %s" % json.dumps(OWNER)))
    for key, want in (("homepage", HOMEPAGE), ("repository", HOMEPAGE), ("license", LICENSE)):
        if item.get(key) != want:
            out.append(problem(where, "bad_metadata", "%s must be %s" % (key, want)))
    if not isinstance(item.get("description"), str) or not item["description"].strip():
        out.append(problem(where, "bad_metadata", "description is empty"))


def check_bundle(root: Path, name: str, item: dict, universe: List[str], seen: Dict[str, List[str]], out: List[Problem]) -> None:
    where = "%s[%s]" % (MARKETPLACE, name)
    if item.get("strict") is not False:
        out.append(problem(where, "wrong_entries", "a bundle must set strict to false"))
    paths = item.get("skills")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        out.append(problem(where, "wrong_entries", "skills must be a list of ./skills/<name> paths"))
        return
    names = []
    for path in paths:
        base = path[len("./skills/"):] if path.startswith("./skills/") else path
        names.append(base)
        if not path.startswith("./skills/") or "/" in base or not base:
            out.append(problem(where, "wrong_entries", "%s is not a ./skills/<name> path" % path))
        elif base not in universe:
            out.append(problem(where, "unknown_skill", "%s is not in catalog.json" % base))
        if not (root / path / "SKILL.md").is_file():
            out.append(problem(where, "missing_path", "%s has no SKILL.md" % path))
        seen.setdefault(base, []).append(name)
    if len(set(names)) != len(names):
        out.append(problem(where, "wrong_entries", "a skill is listed twice"))
    if set(names) != set(BUNDLES[name]) and not any(n not in universe for n in names):
        out.append(problem(where, "wrong_entries", "the skills differ from the %s bundle: %s" % (
            name, ", ".join(sorted(set(names) ^ set(BUNDLES[name]))))))
    agents = item.get("agents")
    if name == "qa-core":
        have = sorted(p.name for p in (root / "agents").glob("*.md"))
        listed = sorted(a[len("./agents/"):] for a in agents if isinstance(a, str) and a.startswith("./agents/")) \
            if isinstance(agents, list) else []
        for gone in sorted(set(listed) - set(have)):
            out.append(problem(where, "missing_path", "./agents/%s does not exist" % gone))
        if listed != have or not isinstance(agents, list) or len(agents) != len(listed):
            out.append(problem(where, "wrong_entries", "agents must list every agents/*.md file (%s)" % ", ".join(have)))
    elif agents:
        out.append(problem(where, "wrong_entries", "only qa-core lists agents; here they load by default"))


def guard_hook_shape(doc: object) -> bool:
    """One PreToolUse group, matcher ``Bash``, with one command handler: the guard script, timeout 10."""
    try:
        groups = doc["hooks"]["PreToolUse"]  # type: ignore[index]
        only_event = list(doc["hooks"]) == ["PreToolUse"]  # type: ignore[index]
        handler = groups[0]["hooks"][0]
        return (only_event and len(groups) == 1 and groups[0]["matcher"] == "Bash" and len(groups[0]["hooks"]) == 1
                and handler.get("type") == "command" and handler.get("command") == GUARD_HOOK_COMMAND
                and handler.get("timeout") == 10 and not set(handler) - {"type", "command", "timeout"})
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def check_guard_plugin(root: Path, out: List[Problem]) -> None:
    """The guard plugin's files: what the host loads (manifest, hooks file, script) and what the script needs (kernel)."""
    base = root / GUARD_PATH
    if not (base / ".claude-plugin" / "plugin.json").is_file():
        out.append(problem(GUARD_MANIFEST, "bad_guard_plugin", "the plugin has no .claude-plugin/plugin.json"))
    for relative in GUARD_FILES:
        if not (base / relative).is_file():
            out.append(problem("%s/%s" % (GUARD_PATH, relative), "bad_guard_plugin", "the guard plugin lacks this file"))
    hooks = base / "hooks" / "hooks.json"
    if hooks.is_file():
        try:
            doc = json.loads(hooks.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            doc = None
        if not guard_hook_shape(doc):
            out.append(problem("%s/hooks/hooks.json" % GUARD_PATH, "bad_guard_plugin",
                               "expected one PreToolUse group (matcher Bash) with one command hook, timeout 10, running %s"
                               % GUARD_HOOK_COMMAND))


def check(root: Path) -> List[Problem]:
    out: List[Problem] = []
    for relative in FORBIDDEN_ROOT:
        if (root / relative).exists():
            out.append(problem(relative, "forbidden_root_file", "the repository root must not hold it"))
    doc = load(root, MARKETPLACE, out)
    if doc is None:
        return sorted(out)
    if reserved(doc.get("name")) or doc.get("name") != "qa-engineering-suite":
        kind = "reserved_name" if reserved(doc.get("name")) else "bad_metadata"
        out.append(problem(MARKETPLACE, kind, "the marketplace name must be qa-engineering-suite"))
    if doc.get("owner") != OWNER:
        out.append(problem(MARKETPLACE, "bad_metadata", "owner must be %s" % json.dumps(OWNER)))
    plugins = doc.get("plugins")
    if not isinstance(plugins, list) or not all(isinstance(p, dict) for p in plugins):
        out.append(problem(MARKETPLACE, "wrong_entries", "plugins must be a list of objects"))
        return sorted(out)
    names = [p.get("name") for p in plugins]
    for name in names:
        if reserved(name):
            out.append(problem(MARKETPLACE, "reserved_name", "%s is a reserved plugin name" % name))
    if names != list(ENTRIES):
        out.append(problem(MARKETPLACE, "wrong_entries", "entries must be %s, in that order" % ", ".join(ENTRIES)))
    by_name = {p.get("name"): p for p in plugins}
    universe = listed_skills(root)
    versions: Dict[str, str] = {}
    for name in ENTRIES:
        item = by_name.get(name)
        if item is None:
            continue
        where = "%s[%s]" % (MARKETPLACE, name)
        check_metadata(where, item, out)
        if not isinstance(item.get("keywords"), list) or not item["keywords"]:
            out.append(problem(where, "bad_metadata", "keywords must be a non-empty list"))
        versions[name] = item.get("version") if isinstance(item.get("version"), str) else ""
        want = "./" + GUARD_PATH if name == "qa-command-guard" else "./"
        if item.get("source") != want:
            out.append(problem(where, "wrong_entries", "source must be %s" % want))
    full = by_name.get("qa-engineering-suite", {})
    if any(key in full for key in COMPONENT_FIELDS):
        out.append(problem(MARKETPLACE, "wrong_entries", "qa-engineering-suite must name no component field"))
    seen: Dict[str, List[str]] = {}
    for name in BUNDLES:
        if name in by_name:
            check_bundle(root, name, by_name[name], universe, seen, out)
    for skill, bundles in sorted(seen.items()):
        if len(bundles) > 1:
            out.append(problem(MARKETPLACE, "bundle_overlap", "%s is in %s" % (skill, " and ".join(bundles))))
    if all(name in by_name for name in BUNDLES):
        for skill in sorted(set(universe) - set(seen)):
            out.append(problem(MARKETPLACE, "bundle_gap", "%s is in neither bundle" % skill))
    guard = load(root, GUARD_MANIFEST, out)
    if guard is not None:
        if guard.get("name") != "qa-command-guard":
            out.append(problem(GUARD_MANIFEST, "wrong_entries", "name must be qa-command-guard"))
        check_metadata(GUARD_MANIFEST, guard, out)
        versions["plugin.json"] = guard.get("version") if isinstance(guard.get("version"), str) else ""
    check_guard_plugin(root, out)
    suite = suite_versions(root)
    if len(suite) > 1:
        out.append(problem("skills", "version_mismatch", "the skills disagree on suite-version: %s" % ", ".join(suite)))
    elif suite:
        pattern = re.compile(r"%s(-[0-9A-Za-z][0-9A-Za-z.-]*)?" % re.escape(suite[0]))
        for name, version in sorted(versions.items()):
            if not pattern.fullmatch(version):
                out.append(problem(MARKETPLACE if name != "plugin.json" else GUARD_MANIFEST, "version_mismatch",
                                   "%s has version %r, not %s or %s-<pre-release>" % (name, version, suite[0], suite[0])))
    if len(set(versions.values())) > 1:
        out.append(problem(MARKETPLACE, "version_mismatch", "entries and the guard manifest differ: %s" % ", ".join(
            "%s=%s" % item for item in sorted(versions.items()))))
    return sorted(set(out))


def main(argv: Sequence[str]) -> int:
    sys.stdout.reconfigure(errors="backslashreplace")
    if len(argv) > 1:
        print("usage: check_packaging.py [root]")
        return 2
    root = Path(argv[0]) if argv else Path(".")
    if not root.is_dir():
        print("check_packaging.py: not a directory: %s" % root)
        return 2
    found = check(root)
    for line in found:
        print(line)
    print("%d problem(s)" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
