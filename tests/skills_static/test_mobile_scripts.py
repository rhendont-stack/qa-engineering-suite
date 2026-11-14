"""The bundled scripts of qa-mobile: ``mobile_static.py`` and ``dynamic_checks.py``, with the references they must agree with.

Each script runs as the skill runs it, isolated (``python3 -I -B``), from a project root made at test time (a copy of F10, or
small configuration files written here). Every hostile input is built inside the test's own temporary directory; the one link
points to ``/etc/hosts`` and is never followed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys

import pytest

from helpers import FIXTURES, REPO, answer_key

SKILL = REPO / "skills" / "qa-mobile"
STATIC = SKILL / "scripts" / "mobile_static.py"
DYNAMIC = SKILL / "scripts" / "dynamic_checks.py"
GRADER = SKILL / "evals" / "grade.py"
F10 = FIXTURES / "mobile-static"
MANIFEST = "android/app/src/main/AndroidManifest.xml"
NSC = "android/app/src/main/res/xml/network_security_config.xml"
PLIST = "ios/App/Info.plist"
EXE = "EXE-01ABCDEFGHJKMNPQRSTVWXYZ00"
APPLE = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
         '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n')
ANDROID = 'xmlns:android="http://schemas.android.com/apk/res/android"'
REMEDIATION = ("It needs a device or emulator and a device-control grant, which this machine does not have. "
               "Run it on a machine that has them, then attach the report with qa ingest.")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(script, *args, cwd):
    done = subprocess.run([sys.executable, "-I", "-B", str(script)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=120, cwd=str(cwd), env={"PATH": os.environ["PATH"], "HOME": str(cwd)})
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


def f10_copy(tmp_path):
    root = tmp_path / "f10"
    shutil.copytree(str(F10), str(root))
    return root


def project(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return root


def located(summary):
    return sorted((item["rule"], item["at"]) for item in summary["listed"])


def manifest(application, components="", sdk="", permissions=""):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<manifest %s>\n%s%s<application %s>\n%s</application>\n</manifest>\n'
            % (ANDROID, sdk, permissions, application, components))


def plist(body):
    return APPLE + '<plist version="1.0">\n<dict>\n' + body + '</dict>\n</plist>\n'


# ----------------------------------------------------------------------------------------------- mobile_static.py on F10

def test_mobile_static_finds_exactly_the_four_seeds_of_f10_by_path_and_line(tmp_path):
    root = f10_copy(tmp_path)
    code, out, err = run(STATIC, "--root", ".", "--out", ".qa/work/mobile/mobile-static.sarif", cwd=root)
    assert (code, out["ok"], out["format"]) == (0, True, "sarif"), err
    assert located(out) == [("ats-arbitrary-loads", PLIST + ":15"), ("backup-enabled", MANIFEST + ":8"),
                            ("cleartext-traffic", NSC + ":3"), ("exported-without-permission", MANIFEST + ":17")], out
    assert out["files"] == {"entitlements": 1, "gradle": 1, "manifest": 1, "network-security-config": 1, "plist": 1}
    assert out["unreadable"] == [] and out["notes"] == [] and out["out"] == ".qa/work/mobile/mobile-static.sarif"
    assert out["sdk"] == [{"manifest": MANIFEST, "min_sdk": 26, "target_sdk": 34, "from": "android/app/build.gradle", "approximate": True}]
    by_at = {item["at"]: item for item in out["listed"]}
    for issue in answer_key("mobile-static")["issues"]:
        item = by_at["%s:%d" % (issue["file"], issue["lines"][0])]
        assert issue["masvs"] in item["masvs"] and issue["mastg"] in item["mastg"] and issue["maswe"] in item["maswe"], issue["id"]
        assert issue["class"].split()[0] in item["cwe"] and item["approximate"] is False, issue["id"]
    report = json.loads((root / ".qa" / "work" / "mobile" / "mobile-static.sarif").read_text(encoding="utf-8"))
    assert report["version"] == "2.1.0" and report["runs"][0]["tool"]["driver"]["name"] == "qa-mobile-static"
    results = report["runs"][0]["results"]
    assert len(results) == 4 and all(set(r["properties"]) == {"approximate", "platform", "masvs", "mastg", "maswe", "cwe"} for r in results)
    assert out["sha256"].startswith("sha256:")


def test_mobile_static_reports_neither_negative_control_of_f10(tmp_path):
    root = f10_copy(tmp_path)
    out = run(STATIC, "--root", ".", cwd=root)[1]
    for control in answer_key("mobile-static")["negative_controls"]:
        first, last = control["lines"]
        hits = [item for item in out["listed"] if item["at"].startswith(control["file"] + ":")
                and first <= int(item["at"].rsplit(":", 1)[1]) <= last]
        assert hits == [], control["id"]
    assert not [item for item in out["listed"] if "MainActivity" in item["message"] or "SyncService" in item["message"]]


def test_mobile_static_json_format_and_paths_relative_to_the_working_directory(tmp_path):
    root = f10_copy(tmp_path)
    code, out, err = run(STATIC, "--root", "android", "--format", "json", "--out", ".qa/work/mobile/mobile-static.json", cwd=root)
    assert code == 0, err
    written = json.loads((root / ".qa" / "work" / "mobile" / "mobile-static.json").read_text(encoding="utf-8"))
    assert written["tool"] == "qa-mobile-static" and [r["at"] for r in written["results"]] == [MANIFEST + ":8", MANIFEST + ":17", NSC + ":3"]
    assert written["results"] == out["listed"] and written["files"]["plist"] == 0


# ----------------------------------------------------------------------------------------------- the rules F10 does not seed

def test_mobile_static_android_rules_beyond_f10(tmp_path):
    components = (
        '<activity android:name=".Open" android:exported="true" />\n'                                   # line 6: no permission
        '<activity android:name=".Implicit">\n<intent-filter>\n<action android:name="x.VIEW" />\n</intent-filter>\n</activity>\n'
        '<service android:name=".Weak" android:exported="true" android:permission="com.example.WEAK" />\n'    # line 12: normal level
        '<receiver android:name=".Strong" android:exported="true" android:permission="com.example.STRONG" />\n'
        '<receiver android:name=".Elsewhere" android:exported="true" android:permission="android.permission.BIND_JOB_SERVICE" />\n'
        '<provider android:name=".Data" android:authorities="com.example.data" android:exported="true" />\n'  # line 15
        '<provider android:name=".ReadOnly" android:authorities="com.example.ro" android:exported="true" '
        'android:readPermission="com.example.STRONG" />\n'
        '<activity android:name=".Closed" android:exported="false" />\n')
    permissions = ('<permission android:name="com.example.WEAK" />\n'
                   '<permission android:name="com.example.STRONG" android:protectionLevel="signature|privileged" />\n')
    root = project(tmp_path / "p", {
        "app/build.gradle.kts": "android {\n    defaultConfig {\n        minSdk = 21\n        targetSdk = 30 // old\n    }\n}\n",
        "app/src/main/AndroidManifest.xml": manifest('android:debuggable="true" android:usesCleartextTraffic="true" '
                                                     'android:dataExtractionRules="@xml/rules"', components, permissions=permissions),
        "lib/src/main/AndroidManifest.xml": manifest('android:allowBackup="false"', sdk='<uses-sdk android:minSdkVersion="16" '
                                                     'android:targetSdkVersion="26" />\n'),
        "lib/src/main/res/xml/network.xml": ('<?xml version="1.0"?>\n<network-security-config>\n<domain-config '
                                             'cleartextTrafficPermitted="true">\n<domain>api.example.com</domain>\n'
                                             '<domain-config cleartextTrafficPermitted="true"><domain>cdn.example.com</domain>'
                                             '</domain-config>\n</domain-config>\n</network-security-config>\n'),
        "lib/src/main/res/xml/other.xml": '<?xml version="1.0"?>\n<paths />\n',
    })
    code, out, err = run(STATIC, "--root", ".", cwd=root)
    assert code == 0, err
    app = "app/src/main/AndroidManifest.xml"
    assert located(out) == [
        ("backup-enabled", app + ":5"), ("cleartext-traffic", app + ":5"), ("cleartext-traffic", "lib/src/main/AndroidManifest.xml:4"),
        ("cleartext-traffic", "lib/src/main/res/xml/network.xml:3"), ("cleartext-traffic", "lib/src/main/res/xml/network.xml:5"),
        ("debuggable", app + ":5"), ("exported-without-permission", app + ":12"), ("exported-without-permission", app + ":15"),
        ("exported-without-permission", app + ":6"), ("exported-without-permission", app + ":7"),
    ], out
    listed = {(i["rule"], i["at"]): i for i in out["listed"]}
    assert listed[("debuggable", app + ":5")]["masvs"] == ["MASVS-RESILIENCE-4", "MASVS-PLATFORM-2"]
    assert listed[("debuggable", app + ":5")]["mastg"] == ["MASTG-TEST-0226"] and listed[("debuggable", app + ":5")]["cwe"] == ["CWE-489"]
    assert "minSdk 21" in listed[("backup-enabled", app + ":5")]["message"] and listed[("backup-enabled", app + ":5")]["approximate"]
    assert listed[("exported-without-permission", app + ":7")]["approximate"] and "targetSdk 30" in listed[("exported-without-permission", app + ":7")]["message"]
    assert listed[("exported-without-permission", app + ":12")]["mastg"] == ["MASTG-TEST-0365"]
    assert "normal (none set)" in listed[("exported-without-permission", app + ":12")]["message"]
    assert listed[("exported-without-permission", app + ":15")]["mastg"] == ["MASTG-TEST-0355"]
    assert not listed[("exported-without-permission", app + ":6")]["approximate"]
    assert listed[("cleartext-traffic", "lib/src/main/AndroidManifest.xml:4")]["approximate"]
    assert "targetSdk 26" in listed[("cleartext-traffic", "lib/src/main/AndroidManifest.xml:4")]["message"]
    assert not [i for i in out["listed"] if i["at"] in (app + ":13", app + ":14", app + ":16", app + ":17")]
    assert "no manifest read here names this file" in listed[("cleartext-traffic", "lib/src/main/res/xml/network.xml:3")]["message"]
    assert "api.example.com" in listed[("cleartext-traffic", "lib/src/main/res/xml/network.xml:3")]["message"]
    assert [n for n in out["notes"] if "BIND_JOB_SERVICE" in n and ":14:" in n]
    assert out["files"]["network-security-config"] == 1 and out["files"]["gradle"] == 1
    assert {s["manifest"]: (s["min_sdk"], s["target_sdk"], s["from"]) for s in out["sdk"]} == {
        app: (21, 30, "app/build.gradle.kts"), "lib/src/main/AndroidManifest.xml": (16, 26, "lib/src/main/AndroidManifest.xml")}


def test_mobile_static_backup_and_cleartext_variants(tmp_path):
    root = project(tmp_path / "p", {
        "a/build.gradle": "android { defaultConfig { minSdkVersion 31\n targetSdkVersion 33 } }\n",
        "a/src/main/AndroidManifest.xml": manifest('android:fullBackupContent="@xml/b" android:networkSecurityConfig="@xml/missing" '
                                                   'android:usesCleartextTraffic="true"'),
        "b/build.gradle": "android { defaultConfig { minSdkVersion 31\n targetSdkVersion 34 } }\n",
        "b/src/main/AndroidManifest.xml": manifest('android:dataExtractionRules="@xml/d"'),
        "c/src/main/AndroidManifest.xml": manifest('android:fullBackupContent="@xml/b" android:dataExtractionRules="@xml/d"'),
    })
    out = run(STATIC, "--root", ".", cwd=root)[1]
    assert located(out) == [("backup-enabled", "a/src/main/AndroidManifest.xml:3")], out
    assert "targetSdk 33" in out["listed"][0]["message"]
    assert [n for n in out["notes"] if "@xml/missing" in n]


def test_mobile_static_ios_rules_beyond_f10(tmp_path):
    info = plist("<key>NSAppTransportSecurity</key>\n<dict>\n"
                 "<key>NSAllowsArbitraryLoads</key>\n<true/>\n"                                      # line 7
                 "<key>NSAllowsArbitraryLoadsInWebContent</key>\n<true/>\n"                          # line 9
                 "<key>NSAllowsLocalNetworking</key>\n<true/>\n"                                     # line 11
                 "<key>NSExceptionDomains</key>\n<dict>\n<key>legacy.example.com</key>\n<dict>\n"
                 "<key>NSExceptionAllowsInsecureHTTPLoads</key>\n<true/>\n</dict>\n"                 # line 17
                 "<key>safe.example.com</key>\n<dict>\n<key>NSIncludesSubdomains</key>\n<true/>\n</dict>\n</dict>\n</dict>\n")
    root = project(tmp_path / "p", {
        "ios/App/Info.plist": info,
        "ios/App/App.entitlements": plist("<key>get-task-allow</key>\n<true/>\n"),
        "ios/Ext/Ext-Info.plist": plistlib.dumps({"NSAppTransportSecurity": {"NSAllowsArbitraryLoadsForMedia": True}},
                                                  fmt=plistlib.FMT_BINARY),
        "ios/Ok/Info.plist": plist("<key>NSAppTransportSecurity</key>\n<dict>\n<key>NSAllowsArbitraryLoads</key>\n<false/>\n</dict>\n"),
    })
    code, out, err = run(STATIC, "--root", ".", cwd=root)
    assert code == 0, err
    assert located(out) == [("ats-arbitrary-loads", "ios/App/Info.plist:9"), ("ats-arbitrary-loads", "ios/Ext/Ext-Info.plist:1"),
                            ("ats-insecure-exception", "ios/App/Info.plist:11"), ("ats-insecure-exception", "ios/App/Info.plist:17"),
                            ("get-task-allow", "ios/App/App.entitlements:5")], out
    listed = {(i["rule"], i["at"]): i for i in out["listed"]}
    assert listed[("get-task-allow", "ios/App/App.entitlements:5")]["mastg"] == ["MASTG-TEST-0261"]
    assert "legacy.example.com" in listed[("ats-insecure-exception", "ios/App/Info.plist:17")]["message"]
    assert [n for n in out["notes"] if "ios/App/Info.plist:7" in n and "ignore" in n]
    assert [n for n in out["notes"] if "binary property list" in n]
    assert out["files"] == {"entitlements": 1, "gradle": 0, "manifest": 0, "network-security-config": 0, "plist": 3}


# ----------------------------------------------------------------------------------------------- Review Focus 5

def test_mobile_static_refuses_entities_and_links(tmp_path):
    """Hostile configuration files: each one ``unreadable`` with its reason, no finding from any of them, exit 0."""
    exported = '<activity android:name=".Share" android:exported="true" />\n'
    entity_manifest = ('<?xml version="1.0"?>\n<!DOCTYPE manifest [\n<!ENTITY on "true">\n]>\n<manifest %s>\n'
                       '<application android:allowBackup="&on;">\n%s</application>\n</manifest>\n' % (ANDROID, exported))
    entity_plist = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd" [\n<!ENTITY yes "true">\n]>\n<plist version="1.0">\n<dict>\n'
                    '<key>NSAppTransportSecurity</key>\n<dict>\n<key>NSAllowsArbitraryLoads</key>\n<true/>\n</dict>\n</dict>\n</plist>\n')
    body = plist("<key>NSAppTransportSecurity</key>\n<dict>\n<key>NSAllowsArbitraryLoads</key>\n<true/>\n</dict>\n").encode("utf-8")
    oversized = body.replace(b"<plist", b"<!--" + b" " * (1024 * 1024 + 1 - len(body) - len(b"<!---->\n")) + b"-->\n<plist", 1)
    assert len(oversized) == 1024 * 1024 + 1
    root = project(tmp_path / "p", {
        "android/app/src/main/AndroidManifest.xml": entity_manifest,
        "ios/App/Info.plist": entity_plist,
        "ios/Big/Info.plist": oversized,
    })
    (root / "ios" / "Linked").mkdir(parents=True)
    os.symlink("/etc/hosts", str(root / "ios" / "Linked" / "Info.plist"))
    code, out, err = run(STATIC, "--root", ".", "--out", ".qa/work/mobile/mobile-static.sarif", cwd=root)
    assert (code, out["ok"], out["results"], out["listed"]) == (0, True, 0, []), (out, err)
    reasons = {u["path"]: u["reason"] for u in out["unreadable"]}
    assert set(reasons) == {"android/app/src/main/AndroidManifest.xml", "ios/App/Info.plist", "ios/Big/Info.plist", "ios/Linked/Info.plist"}
    assert reasons["android/app/src/main/AndroidManifest.xml"].startswith("a DOCTYPE declaration")
    assert "internal subset" in reasons["ios/App/Info.plist"] and "entities" in reasons["ios/App/Info.plist"]
    assert reasons["ios/Big/Info.plist"] == "over 1 MiB"
    assert reasons["ios/Linked/Info.plist"] == "a link, not followed"
    assert out["files"] == {"entitlements": 0, "gradle": 0, "manifest": 0, "network-security-config": 0, "plist": 0}
    report = json.loads((root / ".qa" / "work" / "mobile" / "mobile-static.sarif").read_text(encoding="utf-8"))["runs"][0]
    assert report["results"] == [] and report["properties"]["unreadable"] == out["unreadable"]
    assert len(report["invocations"][0]["toolExecutionNotifications"]) == 4


def test_mobile_static_refuses_other_doctypes_and_undeclared_entities(tmp_path):
    allowing = "<key>NSAppTransportSecurity</key>\n<dict>\n<key>NSAllowsArbitraryLoads</key>\n<true/>\n</dict>\n"
    root = project(tmp_path / "p", {
        "a/Info.plist": APPLE.replace("-//Apple//DTD PLIST 1.0//EN", "-//Example//DTD PLIST 1.0//EN") + '<plist version="1.0">\n<dict>\n'
                        + allowing + "</dict>\n</plist>\n",
        "b/Info.plist": plist("<key>name</key>\n<string>&undeclared;</string>\n" + allowing),
        "c/App.entitlements": '<?xml version="1.0"?>\n<!DOCTYPE plist SYSTEM "file:///etc/hosts">\n<plist><dict/></plist>\n',
        "d/src/main/res/xml/net.xml": '<?xml version="1.0"?>\n<!DOCTYPE network-security-config>\n<network-security-config>\n'
                                      '<base-config cleartextTrafficPermitted="true" />\n</network-security-config>\n',
        "e/src/main/AndroidManifest.xml": "<manifest><application android:debuggable='true'></manifest>\n",
        "f/Info.plist": APPLE + "<plist><array><string>x</string></array></plist>\n",
    })
    code, out, err = run(STATIC, "--root", ".", cwd=root)
    assert (code, out["results"]) == (0, 0), (out, err)
    reasons = {u["path"]: u["reason"] for u in out["unreadable"]}
    assert reasons["a/Info.plist"] == "a DOCTYPE other than Apple's public plist identifier"
    assert reasons["b/Info.plist"] == "a reference to an undeclared entity"
    assert reasons["c/App.entitlements"] == "a DOCTYPE other than Apple's public plist identifier"
    assert reasons["d/src/main/res/xml/net.xml"].startswith("a DOCTYPE declaration")
    assert reasons["e/src/main/AndroidManifest.xml"].startswith("not well-formed XML")
    assert reasons["f/Info.plist"] == "the property list is not a dictionary"


def test_mobile_static_lists_a_linked_directory_and_never_enters_it(tmp_path):
    outside = project(tmp_path / "outside", {"Info.plist": plist("<key>get-task-allow</key>\n<true/>\n")})
    root = project(tmp_path / "p", {"README.md": "x\n"})
    os.symlink(str(outside), str(root / "ios"))
    out = run(STATIC, "--root", ".", cwd=root)[1]
    assert out["listed"] == [] and out["unreadable"] == [{"path": "ios", "reason": "a link, not followed"}]


# ----------------------------------------------------------------------------------------------- writing

def test_mobile_static_writes_only_under_its_work_folder_or_the_evidence_folder(tmp_path):
    root = f10_copy(tmp_path)
    for out_path in ("report.sarif", ".qa/work/frontend/x.sarif", ".qa/work/mobile/../x.sarif", "/tmp/x.sarif", "/qa-evidence/sub/x.sarif"):
        code, out, _ = run(STATIC, "--root", ".", "--out", out_path, cwd=root)
        assert (code, out["ok"]) == (2, False), out_path
    assert not (root / "report.sarif").exists() and not (root / ".qa" / "work" / "frontend").exists()
    (root / "linked").symlink_to(root / "android")
    assert run(STATIC, "--root", "linked", cwd=root)[0] == 2
    assert run(STATIC, cwd=root)[0] == 2
    os.makedirs(str(root / ".qa" / "work"))
    os.symlink(str(tmp_path), str(root / ".qa" / "work" / "mobile"))
    code, out, _ = run(STATIC, "--root", ".", "--out", ".qa/work/mobile/m.sarif", cwd=root)
    assert (code, out["ok"]) == (2, False) and not (tmp_path / "m.sarif").exists()


def test_mobile_static_copy_to_work_is_byte_equal(tmp_path):
    root = f10_copy(tmp_path)
    code, out, err = run(STATIC, "--copy-to-work", cwd=root)
    assert code == 0, err
    assert out["input"] == "/work/.qa-input/mobile/mobile_static.py" and out["written"] == ".qa/work/mobile/mobile_static.py"
    assert (root / ".qa" / "work" / "mobile" / "mobile_static.py").read_bytes() == STATIC.read_bytes()
    assert run(STATIC, "--copy-to-work", "--root", ".", cwd=root)[0] == 2


# ----------------------------------------------------------------------------------------------- dynamic_checks.py

def test_dynamic_checks_lists_the_six_device_tests_not_run_with_the_remediation(tmp_path):
    root = f10_copy(tmp_path)
    code, out, err = run(DYNAMIC, cwd=root)
    assert (code, out["ok"], out["checks"], out["status"]) == (0, True, 6, "not_run"), err
    table = json.loads((root / ".qa" / "work" / "mobile" / "dynamic-checks.json").read_text(encoding="utf-8"))
    grader = load(GRADER, "qa_mobile_grade_for_scripts")
    assert [c["test"] for c in table["checks"]] == list(grader.DEVICE_TESTS)
    for check in table["checks"]:
        platform, title, weakness = grader.DEVICE_TESTS[check["test"]]
        assert (check["platform"], check["title"], check["maswe"]) == (platform, title, weakness), check["test"]
        assert check["status"] == "not_run" and check["execution"] is None and check["remediation"] == REMEDIATION
        assert check["url"].startswith("https://mas.owasp.org/MASTG/tests/") and check["url"].endswith("/%s/" % check["test"])
    assert [n["test"] for n in table["not_listed"]] == list(grader.PLACEHOLDER_TESTS)
    assert grader.table_problems(grader.table_entries(table)) == []
    assert grader.body_device_claims(json.dumps(table)) == [] and not grader.EXE.search(json.dumps(table))


def test_dynamic_checks_blocked_by_the_device_run(tmp_path):
    root = f10_copy(tmp_path)
    code, out, err = run(DYNAMIC, "--blocked-execution", EXE, cwd=root)
    assert (code, out["status"], out["execution"]) == (0, "blocked", EXE), err
    table = json.loads((root / ".qa" / "work" / "mobile" / "dynamic-checks.json").read_text(encoding="utf-8"))
    assert {(c["status"], c["execution"]) for c in table["checks"]} == {("blocked", EXE)}
    assert all(EXE in c["reason"] and "device-control" in c["reason"] for c in table["checks"])
    grader = load(GRADER, "qa_mobile_grade_for_scripts_blocked")
    assert grader.table_problems(grader.table_entries(table)) == [] and grader.body_device_claims(json.dumps(table)) == []
    for bad in ("EXE-1", "passed", EXE.lower()):
        assert run(DYNAMIC, "--blocked-execution", bad, cwd=root)[0] == 2, bad
    assert run(DYNAMIC, "--status", "passed", cwd=root)[0] == 2


def test_dynamic_checks_never_writes_through_a_link(tmp_path):
    root = f10_copy(tmp_path)
    os.makedirs(str(root / ".qa" / "work"))
    os.symlink(str(tmp_path), str(root / ".qa" / "work" / "mobile"))
    code, out, _ = run(DYNAMIC, cwd=root)
    assert (code, out["ok"]) == (2, False) and not (tmp_path / "dynamic-checks.json").exists()


# ----------------------------------------------------------------------------------------------- what the skill's files say

def test_no_device_result_or_execution_id_in_what_the_skill_writes(tmp_path):
    """The script copy, the SARIF and the table land in .qa/work/mobile/: none states a device result or names an EXE."""
    grader = load(GRADER, "qa_mobile_grade_for_texts")
    root = f10_copy(tmp_path)
    run(STATIC, "--copy-to-work", cwd=root)
    run(STATIC, "--root", ".", "--out", ".qa/work/mobile/mobile-static.sarif", cwd=root)
    run(DYNAMIC, cwd=root)
    for path in sorted((root / ".qa" / "work" / "mobile").iterdir()):
        text = path.read_text(encoding="utf-8")
        assert grader.body_device_claims(text) == [], path.name
        assert not grader.EXE.search(text), path.name
    for name in ("SKILL.md", "references/dynamic-checks.md", "references/tools.md", "references/triage.md"):
        assert grader.device_claims((SKILL / name).read_text(encoding="utf-8")) == [], name


def test_the_references_agree_with_the_scripts():
    static = load(STATIC, "qa_mobile_static_for_refs")
    dynamic = load(DYNAMIC, "qa_mobile_dynamic_for_refs")
    mapping = (SKILL / "references" / "masvs-mapping.md").read_text(encoding="utf-8")
    checks = (SKILL / "references" / "dynamic-checks.md").read_text(encoding="utf-8")
    for rule, info in static.RULES.items():
        row = next(line for line in mapping.splitlines() if line.startswith("| `%s` |" % rule))
        for key in ("masvs", "mastg", "maswe", "cwe"):
            for identifier in info[key]:
                assert "`%s`" % identifier in row, (rule, identifier)
    used = {i for info in static.RULES.values() for key in ("masvs", "mastg", "maswe") for i in info[key]}
    used |= {i for c in dynamic.CHECKS for i in [c[0], c[4]] + c[5]}
    for identifier in used:
        assert identifier in mapping or identifier in checks, identifier
    for identifier in re.findall(r"MAS(?:VS-[A-Z]+-\d|TG-TEST-\d{4}|WE-\d{4})", mapping):
        assert re.search(r"https://mas\.owasp\.org/\S*%s/" % re.escape(identifier), mapping + checks) or identifier.startswith("MASVS-"), identifier
    for test, platform, category, title, weakness, _masvs, _rules, _steps in dynamic.CHECKS:
        row = next(line for line in checks.splitlines() if line.startswith("| `%s` |" % test))
        assert title in row and "`%s`" % weakness in row and "/%s/%s/" % (category, test) in row, test
    assert "\n> " + REMEDIATION[:40] in checks and REMEDIATION in checks.replace("\n> ", " ") and dynamic.REMEDIATION == REMEDIATION
    assert "MASTG-TEST-0238" in checks
    tools = (SKILL / "references" / "tools.md").read_text(encoding="utf-8")
    images = json.loads((REPO / "shared" / "policies" / "container-images.json").read_text(encoding="utf-8"))["images"]
    python = next(entry["image"] for entry in images if entry["id"] == "python-newest")
    legs = dict((m.group(1), m.group(2)) for m in re.finditer(r"^```qa-leg (\S+)[^\n]*\n(.*?)\n```$", tools, re.M | re.S))
    assert set(legs) == {"mobile-static", "device-check"}
    assert all("--container %s " % python in line for line in legs.values())
    assert legs["device-check"].endswith("-- adb devices")
    formats = re.findall(r"`ingest (\w+)", tools)
    cli = (REPO / "shared" / "kernel" / "qa" / "cli.py").read_text(encoding="utf-8")
    choices = re.search(r'attach\.add_argument\("format", choices=\(([^)]*)\)', cli).group(1)
    assert formats and all('"%s"' % f in choices for f in formats + re.findall(r"`(\w+)`", tools.split("The kernel's other formats")[1].split(")")[0]))


@pytest.mark.parametrize("script", [STATIC, DYNAMIC])
def test_scripts_import_only_the_standard_library(script):
    allowed = {"__future__", "argparse", "hashlib", "importlib", "json", "os", "plistlib", "re", "secrets", "stat", "sys", "pathlib",
               "typing", "xml"}
    imported = set(re.findall(r"^(?:from|import) ([\w]+)", script.read_text(encoding="utf-8"), re.M))
    assert imported <= allowed, imported - allowed
