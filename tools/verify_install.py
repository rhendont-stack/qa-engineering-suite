"""Installs every marketplace entry with the real ``claude`` CLI in a scratch configuration and checks what loaded.

Usage: python3 -I tools/verify_install.py --out DIR [--claude PATH] [--commit REV] [--root REPO]

Exports ``REV`` (default HEAD) of the repository with ``git archive``, runs ``claude plugin validate --strict`` on the export
and on ``plugins/qa-command-guard``, adds the export as a marketplace, then installs each entry and reads its component
inventory with ``claude plugin details``. The inventory must equal ``expected_inventory`` (derived from the marketplace and
the catalog). Writes ``DIR/install.json`` and the raw CLI outputs beside it, and prints a one-line JSON summary.

Every CLI call gets an environment built from scratch: PATH, a temporary HOME and CLAUDE_CONFIG_DIR, and the two switches that
turn telemetry and non-essential traffic off. Nothing is copied from the caller's environment, so the caller's Claude Code
configuration and any credential variable are never read or reachable. The temporary directory is removed afterwards.

Exit 0 when ``ok``, 1 when not, 2 for bad usage, 3 when the CLI or the revision cannot be used (including a marketplace
or catalog that is not the expected shape). A ``details`` call that exits non-zero counts as not matching.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
MARKETPLACE = ".claude-plugin/marketplace.json"
TIMEOUT = 300
COUNT = re.compile(r"^\s*(Skills|Agents|Hooks) \((\d+)\)[ \t]*(.*?)\s*$", re.M)


def _names(items: List[str]) -> List[str]:
    return sorted(set(items))


def _hook_count(path: Path) -> int:
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
        return sum(len(group.get("hooks", [])) for groups in events.values() for group in groups)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def expected_inventory(root: Path) -> Dict[str, Dict[str, object]]:
    """``{entry: {"skills": [names], "agents": [names], "hooks": count}}`` from marketplace.json and catalog.json."""
    market = json.loads((root / MARKETPLACE).read_text(encoding="utf-8"))
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))["skills"]
    result: Dict[str, Dict[str, object]] = {}
    for item in market["plugins"]:
        source = (root / item["source"]).resolve()
        if "skills" in item:
            skills = [Path(p).name for p in item["skills"]]
        elif source == root.resolve():
            skills = list(catalog)
        else:
            skills = [p.parent.name for p in (source / "skills").glob("*/SKILL.md")]
        if item.get("agents"):
            agents = [Path(p).stem for p in item["agents"]]
        else:  # an absent or empty list leaves the default agents/ scan in force
            agents = [p.stem for p in (source / "agents").glob("*.md")]
        result[item["name"]] = {"skills": _names(skills), "agents": _names(agents),
                                "hooks": _hook_count(source / "hooks" / "hooks.json")}
    return result


def parse_details(text: str) -> Optional[Dict[str, object]]:
    """The component inventory in ``claude plugin details`` output, or None when it has none."""
    found: Dict[str, object] = {}
    for kind, count, rest in COUNT.findall(text):
        key = kind.lower()
        found[key] = int(count) if key == "hooks" else _names([n.strip() for n in rest.split(",") if n.strip()])
    return found if set(found) == {"skills", "agents", "hooks"} else None


def scratch_env(tmp: Path) -> Dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp / "home"), "CLAUDE_CONFIG_DIR": str(tmp / "cfg"),
            "DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def run_claude(claude: str, args: List[str], tmp: Path) -> Tuple[int, str]:
    try:
        done = subprocess.run([claude] + args, cwd=str(tmp), env=scratch_env(tmp), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "timed out after %d seconds\n" % TIMEOUT
    text = done.stdout.decode("utf-8", "replace")
    for form in (str(tmp.resolve()), str(tmp)):  # the resolved form first: on macOS it holds the plain one (/private/var/...)
        text = text.replace(form, "<TMP>")
    return done.returncode, text


def export(root: Path, rev: str, target: Path, tmp: Path) -> str:
    git_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp / "home")}
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", rev + "^{commit}"], env=git_env,
                            capture_output=True, text=True, timeout=60)
    if commit.returncode != 0:
        raise ValueError("not a commit: %s" % rev)
    archive = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", commit.stdout.strip()], env=git_env,
                             capture_output=True, timeout=120, check=True)
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        if hasattr(tarfile, "data_filter"):  # 3.12+, and the security releases of earlier versions
            tar.extractall(str(target), filter="data")
        else:
            tar.extractall(str(target))
    return commit.stdout.strip()


def tree_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def verify(root: Path, rev: str, claude: str, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="qa-verify-install-"))
    try:
        (tmp / "home").mkdir()
        (tmp / "cfg").mkdir()
        source = tmp / "src" / "qa-engineering-suite"
        commit = export(root, rev, source, tmp)
        try:
            market = json.loads((source / MARKETPLACE).read_text(encoding="utf-8"))["name"]
            expected = expected_inventory(source)
        except (KeyError, TypeError, AttributeError, ValueError, IndexError) as err:
            raise ValueError("the marketplace or the catalog is malformed: %s: %s" % (type(err).__name__, err))

        def call(label: str, args: List[str]) -> Tuple[int, str]:
            code, text = run_claude(claude, args, tmp)
            (out / (label + ".txt")).write_text(text, encoding="utf-8")
            return code, label + ".txt"

        doc: dict = {"commit": commit, "validate": {}, "entries": {}}
        for target, path in (("marketplace", source), ("qa-command-guard", source / "plugins" / "qa-command-guard")):
            code, saved = call("validate-" + target, ["plugin", "validate", "--strict", str(path)])
            doc["validate"][target] = {"exit": code, "output_path": saved}
        added, _ = call("marketplace-add", ["plugin", "marketplace", "add", str(source)])
        ok = added == 0 and all(v["exit"] == 0 for v in doc["validate"].values())
        for name, want in expected.items():
            full = "%s@%s" % (name, market)
            installed, _ = call("install-" + name, ["plugin", "install", full])
            shown, details_path = call("details-" + name, ["plugin", "details", full])
            found = parse_details((out / details_path).read_text(encoding="utf-8")) if installed == 0 and shown == 0 else None
            matches = found is not None and found == want
            ok = ok and installed == 0 and shown == 0 and matches
            doc["entries"][name] = {
                "installed": installed == 0,
                "skills": len(found["skills"]) if found else None,  # type: ignore[arg-type]
                "agents": len(found["agents"]) if found else None,  # type: ignore[arg-type]
                "hooks": found["hooks"] if found else None,
                "size_bytes": tree_size(tmp / "cfg" / "plugins" / "cache" / market / name),
                "details_path": details_path, "matches_expected": matches}
        doc["ok"] = ok
        (out / "install.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return doc
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="verify_install.py", description="Install every marketplace entry and check its inventory.")
    parser.add_argument("--out", required=True, help="directory for install.json and the raw CLI outputs")
    parser.add_argument("--claude", help="the claude executable (default: the one on PATH)")
    parser.add_argument("--commit", default="HEAD", help="revision to export (default HEAD)")
    parser.add_argument("--root", default=str(REPO), help="repository to export (default: this one)")
    args = parser.parse_args(argv)
    claude = args.claude or shutil.which("claude")
    if not claude or not os.access(claude, os.X_OK):
        print("verify_install.py: refused: no usable claude executable")
        return 3
    try:
        doc = verify(Path(args.root), args.commit, claude, Path(args.out))
    except (ValueError, subprocess.CalledProcessError, OSError, tarfile.TarError) as err:
        print("verify_install.py: refused: %s" % err)
        return 3
    print(json.dumps({"ok": doc["ok"], "commit": doc["commit"], "out": str(Path(args.out) / "install.json")}))
    return 0 if doc["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
