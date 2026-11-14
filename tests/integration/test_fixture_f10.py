"""F10 ``mobile-static``: Android and iOS configuration files with an answer key. There is no app code and nothing is ever built.

The key names each seed by class (MASVS control and category). The fixture's own files never say what is wrong with them.
"""
from __future__ import annotations

import json
import plistlib
import re
from xml.parsers import expat

import pytest

from helpers import FIXTURES, answer_key

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
