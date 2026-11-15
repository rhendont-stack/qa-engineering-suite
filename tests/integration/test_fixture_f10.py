"""F10 ``mobile-static``: Android and iOS configuration files with an answer key. There is no app code and nothing is ever built.

The key names each seed by class (MASVS control and category). The fixture's own files never say what is wrong with them.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
from xml.parsers import expat

import pytest

from helpers import FIXTURES, LAUNCHER, REPO, answer_key

F10 = FIXTURES / "mobile-static"
ANDROID = F10 / "android" / "app" / "src" / "main"
XML_FILES = (ANDROID / "AndroidManifest.xml", ANDROID / "res" / "xml" / "network_security_config.xml", F10 / "ios" / "App" / "Info.plist")
PLIST_FILES = (F10 / "ios" / "App" / "Info.plist", F10 / "ios" / "App" / "App.entitlements")
APPLE_PUBLIC_ID = "-//Apple//DTD PLIST 1.0//EN"
TOOLS = {"mobile-static", "static-review"}
EXPECTED_FILES = {
    "README.md", "android/app/build.gradle", "android/app/src/main/AndroidManifest.xml",
    "android/app/src/main/res/xml/network_security_config.xml", "ios/App/Info.plist", "ios/App/App.entitlements",
}


def parse_strictly(path):
    """Parse as the static checker will: no DOCTYPE but Apple's public plist one without an internal subset, and no entity declaration."""
    seen = {"doctype": []}
    parser = expat.ParserCreate()

    def start_doctype(name, system_id, public_id, has_internal_subset):
        seen["doctype"].append((name, public_id, has_internal_subset))
        if public_id != APPLE_PUBLIC_ID or has_internal_subset:
            raise ValueError("a DOCTYPE other than Apple's public plist identifier: %s" % path.name)

    def entity(*_args):
        raise ValueError("entity declaration in %s" % path.name)

    parser.StartDoctypeDeclHandler = start_doctype
    parser.EntityDeclHandler = entity
    parser.Parse(path.read_bytes(), True)
    return seen


def test_f10_files_are_the_planned_ones_and_none_is_empty():
    found = {p.relative_to(F10).as_posix() for p in F10.rglob("*") if p.is_file()}
    assert found == EXPECTED_FILES
    for name in found:
        assert (F10 / name).read_text(encoding="utf-8").strip(), name


def test_f10_files_parse_with_the_stdlib():
    for path in XML_FILES:
        seen = parse_strictly(path)
        assert (len(seen["doctype"]) == 1) == (path.name == "Info.plist"), path.name
    for path in PLIST_FILES:
        with path.open("rb") as handle:
            assert isinstance(plistlib.load(handle), dict), path.name
    assert not [p for p in F10.rglob("*") if p.is_file() and "<!ENTITY" in p.read_text(encoding="utf-8")]


def test_f10_answer_key_locations_exist(repo_root):
    key = answer_key("mobile-static")
    anchors = json.loads((repo_root / "shared" / "policies" / "severity-rubric.json").read_text(encoding="utf-8"))["anchors"]
    categories = set(json.loads((repo_root / "shared" / "schemas" / "defs.schema.json").read_text(encoding="utf-8"))["$defs"]["category"]["enum"])
    issues = key["issues"]
    assert key["fixture"] == "mobile-static" and "suites" not in key
    assert [issue["id"] for issue in issues] == ["F10-01", "F10-02", "F10-03", "F10-04"]
    for issue in issues:
        required = {"id", "location", "file", "lines", "match", "category", "severity_anchor", "class", "detectable_by", "masvs", "mastg"}
        assert required <= set(issue) <= required | {"maswe", "mastg_dynamic"}, issue["id"]
        path = F10 / issue["file"]
        assert path.is_file(), issue["id"]
        first, last = issue["lines"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= first <= last <= len(lines), issue["id"]
        assert issue["match"] in "\n".join(lines[first - 1:last]), issue["id"]
        assert issue["severity_anchor"] in anchors and issue["category"] in categories, issue["id"]
        assert issue["class"] and issue["detectable_by"] and set(issue["detectable_by"]) <= TOOLS, issue["id"]
        assert re.fullmatch(r"MASVS-[A-Z]+-\d", issue["masvs"]) and re.fullmatch(r"MASTG-TEST-\d{4}", issue["mastg"]), issue["id"]
        assert re.fullmatch(r"MASWE-\d{4}", issue.get("maswe", "MASWE-0000")), issue["id"]
        assert "mobile-static" in issue["detectable_by"], issue["id"]
    assert [issue["masvs"] for issue in issues] == ["MASVS-NETWORK-1", "MASVS-PLATFORM-1", "MASVS-NETWORK-1", "MASVS-STORAGE-2"]


def test_f10_negative_controls_are_marked_in_the_key():
    key = answer_key("mobile-static")
    controls = key["negative_controls"]
    assert [control["id"] for control in controls] == ["F10-N1", "F10-N2"]
    manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8").splitlines()
    for control in controls:
        assert set(control) == {"id", "file", "lines", "match", "why_clean"} and control["why_clean"], control["id"]
        assert control["file"] == "android/app/src/main/AndroidManifest.xml"
        first, last = control["lines"]
        assert control["match"] in "\n".join(manifest[first - 1:last]), control["id"]
    seeded = {tuple(issue["lines"]) for issue in key["issues"]}
    assert not [c for c in controls if tuple(c["lines"]) in seeded]
    assert "android.intent.category.LAUNCHER" in "\n".join(manifest[controls[0]["lines"][0] - 1:controls[0]["lines"][1]])
    assert "android:permission" in "\n".join(manifest[controls[1]["lines"][0] - 1:controls[1]["lines"][1]])


def test_f10_does_not_trigger_the_rules_that_seed_nothing():
    manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert "debuggable" not in manifest and "usesCleartextTraffic" not in manifest
    with (F10 / "ios" / "App" / "App.entitlements").open("rb") as handle:
        assert not plistlib.load(handle).get("get-task-allow")
    with (F10 / "ios" / "App" / "Info.plist").open("rb") as handle:
        ats = plistlib.load(handle)["NSAppTransportSecurity"]
    assert ats["NSAllowsArbitraryLoads"] is True
    assert "NSExceptionDomains" not in ats
    gradle = (F10 / "android" / "app" / "build.gradle").read_text(encoding="utf-8")
    assert "Version" not in gradle and "dependencies" not in gradle and "plugins" not in gradle


def test_f10_files_load_nothing_from_a_real_host():
    for path in F10.rglob("*"):
        if path.is_file():
            urls = re.findall(r"https?://[^\s\"'<>]+", path.read_text(encoding="utf-8"))
            allowed = ("http://schemas.android.com/", "http://www.apple.com/DTDs/", "https://example.com", "https://api.example.com")
            assert not [u for u in urls if not u.startswith(allowed)], path


# ----------------------------------------------------------------------------------------------- the qa-mobile legs
# Each leg's command is read from the ``qa-leg`` blocks of qa-mobile's references/tools.md, the one text the skill follows, and
# run through the real CLI in a copy of F10 with its own workspace, left untrusted as the skill leaves it: a container run needs
# no trust. No grant is recorded: the static leg needs none, and the device check needs one that is never given.

MOBILE = REPO / "skills" / "qa-mobile"
MOBILE_TOOLS_MD = MOBILE / "references" / "tools.md"
MOBILE_PREFIX = ["python3", ".claude/skills/qa-mobile/scripts/qa.py", "--role", "verifier", "--skill", "qa-mobile"]
SEED_RULES = {"F10-01": "cleartext-traffic", "F10-02": "exported-without-permission", "F10-03": "ats-arbitrary-loads",
              "F10-04": "backup-enabled"}


def mobile_legs():
    """``{name: (attributes, the command line)}`` from the ``qa-leg`` fenced blocks of qa-mobile's tools.md."""
    found = {}
    for match in re.finditer(r"^```qa-leg (\S+)((?: [\w-]+=\S+)*)\n(.*?)\n```$", MOBILE_TOOLS_MD.read_text(encoding="utf-8"), re.M | re.S):
        assert match.group(1) not in found, match.group(1)
        found[match.group(1)] = (dict(a.split("=", 1) for a in match.group(2).split()), match.group(3).strip())
    return found


def mobile_words(name):
    attributes, line = mobile_legs()[name]
    words = shlex.split(line)
    assert words[:len(MOBILE_PREFIX)] == MOBILE_PREFIX, words[:len(MOBILE_PREFIX)]
    return attributes, words[len(MOBILE_PREFIX):]


def mobile_cli(root, words, timeout=900):
    done = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(root), "--role", "verifier", "--skill", "qa-mobile", *words],
                          cwd=str(root), capture_output=True, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


def mobile_script(root, name, *args):
    done = subprocess.run([sys.executable, "-I", "-B", str(MOBILE / "scripts" / name), *args], cwd=str(root), capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0 and json.loads(done.stdout)["ok"], done.stdout + done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def f10_project(qa_on_fixture):
    """A copy of F10 in its own untrusted workspace, with the container engine set; skipped without one."""
    engine = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    project = qa_on_fixture("mobile-static")
    assert project.cli("config", "set", "container.engine", engine, "--approval", "fixture test")[0] == 0
    code, out = project.cli("config", "set", "project.trust", "untrusted", "--approval", "fixture test")
    assert code == 0 and project.cli("config", "show")[1]["config"]["project"]["trust"] == "untrusted", out
    return project


def seed_finding(issue, item):
    """The finding the skill records for one check result, as references/triage.md's template writes it."""
    path, line = item["at"].rsplit(":", 1)
    return {
        "title": "%s: %s (%s)" % (item["at"], item["message"].split(":")[0], issue["masvs"]),
        "description": "%s. Read from the configuration by the static check; no device check was run." % item["message"],
        "category": "security",
        "severity_rationale": {"anchor": issue["severity_anchor"], "modifiers": []},
        "component": {"path": path, "criticality": "normal"},
        "verification_status": "suspected",
        "evidence": [item["at"], ".qa/work/mobile/mobile-static.sarif"],
        "reproduction_steps": ["Open %s at line %s" % (path, line)],
        "impact": "See the MASVS control %s." % issue["masvs"],
        "probable_cause": "%s %s" % (item["at"], item["message"]),
        "recommendation": "Change the setting at %s; nothing was changed here." % item["at"],
        "refs": {"rule": item["rule"], "location": path, "masvs": item["masvs"], "mastg": item["mastg"], "maswe": item["maswe"],
                 "cwe": item["cwe"]},
    }


def execution_of(project, execution):
    return project.cli("state", "show", "executions")[1]["state"][execution]


@pytest.mark.docker
def test_f10_mobile_static_leg(f10_project):
    project = f10_project
    issues = {issue["id"]: issue for issue in answer_key("mobile-static")["issues"]}
    scan = mobile_script(project.root, "mobile_static.py", "--root", ".", "--out", ".qa/work/mobile/mobile-static.sarif")
    by_rule = {item["rule"]: item for item in scan["listed"]}
    assert sorted(by_rule) == sorted(SEED_RULES.values()) and scan["results"] == 4, scan
    yours = {}
    for entry, rule in sorted(SEED_RULES.items()):
        name = ".qa/work/mobile/%s.json" % entry.lower()
        (project.root / name).write_text(json.dumps(seed_finding(issues[entry], by_rule[rule])), encoding="utf-8")
        code, added = mobile_cli(project.root, ["finding", "add", "--json", name])
        assert code == 0, added
        yours[rule] = added["id"]
    mobile_script(project.root, "mobile_static.py", "--copy-to-work")
    attributes, words = mobile_words("mobile-static")
    assert attributes == {"grant": "none", "report": "sarif"}
    code, out = mobile_cli(project.root, words)
    print("mobile-static leg:", out["execution"], "exit", code, out["status"], out.get("reason"), out.get("findings"))
    assert (code, out["status"]) == (1, "failed"), out
    assert out["findings"] == {"opened": 0, "updated": 4}, out
    run = execution_of(project, out["execution"])
    assert run["container"]["network_mode"] == "none" and run["reports"][0]["files"] == ["reports/mobile-static.sarif"]
    report = json.loads((project.root / ".qa" / "evidence" / out["execution"] / "reports" / "mobile-static.sarif").read_text(encoding="utf-8"))
    found = sorted((r["ruleId"], r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                    r["locations"][0]["physicalLocation"]["region"]["startLine"]) for r in report["runs"][0]["results"])
    print("mobile-static results:", found)
    assert found == sorted((SEED_RULES[i], issues[i]["file"], issues[i]["lines"][0]) for i in SEED_RULES)
    findings = project.cli("state", "show", "findings")[1]["state"]
    assert sorted(findings) == sorted(yours.values())
    for rule, fid in yours.items():
        finding = findings[fid]
        assert (finding["verification_status"], finding["category"], finding["refs"]["masvs"]) == ("suspected", "security", by_rule[rule]["masvs"])
        assert (finding["source"]["tool"], finding["source"]["start_line"]) == ("qa-mobile-static", int(by_rule[rule]["at"].rsplit(":", 1)[1])), rule
    authorizations = project.cli("state", "show", "authorizations")[1]["state"]
    assert authorizations == {} and project.cli("config", "show")[1]["config"]["project"]["trust"] == "untrusted"


@pytest.mark.docker
def test_f10_device_check_is_blocked(f10_project):
    project = f10_project
    attributes, words = mobile_words("device-check")
    assert attributes == {"grant": "device-control", "report": "none"} and words[-3:] == ["--", "adb", "devices"]
    code, out = mobile_cli(project.root, words)
    print("device-check leg:", out.get("execution"), "exit", code, out.get("status"), out.get("reason"), out.get("limitations"))
    assert (code, out["status"], out["reason"]) == (3, "blocked", "not_authorized"), out
    assert "device-control" in json.dumps(out), out
    run = execution_of(project, out["execution"])
    print("device-check record:", json.dumps({k: run.get(k) for k in ("status", "reason", "exit_code", "container", "authorizations")},
                                             sort_keys=True)[:2000])
    assert run["status"] == "blocked" and not (run.get("container") or {}).get("id")
    assert project.cli("state", "show", "authorizations")[1]["state"] == {}
    table = mobile_script(project.root, "dynamic_checks.py", "--blocked-execution", out["execution"])
    assert (table["status"], table["execution"], table["checks"]) == ("blocked", out["execution"], 6)
    written = json.loads((project.root / ".qa" / "work" / "mobile" / "dynamic-checks.json").read_text(encoding="utf-8"))
    assert {(c["status"], c["execution"]) for c in written["checks"]} == {("blocked", out["execution"])}
