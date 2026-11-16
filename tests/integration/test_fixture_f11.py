"""F11 ``pipeline``: two GitHub Actions workflows, a Dockerfile and a README, all inert and never pushed anywhere.

The key names each seed by class (CWE or the plain class). The fixture's own files never say what is wrong with them.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from urllib.parse import urlparse

import pytest

from helpers import FIXTURES, LAUNCHER, REPO, answer_key

F11 = FIXTURES / "pipeline"
WORKFLOWS = F11 / ".github" / "workflows"
CI, PREVIEW = WORKFLOWS / "ci.yml", WORKFLOWS / "pr-preview.yml"
TOOLS = {"actionlint", "zizmor", "static-review"}
EXPECTED_FILES = {".github/workflows/ci.yml", ".github/workflows/pr-preview.yml", "Dockerfile", "README.md"}
SKIPPED_DIRECTORIES = {".git", ".superpowers", "node_modules", "__pycache__"}


def lines_of(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_f11_files_are_the_planned_ones_and_none_is_empty():
    found = {p.relative_to(F11).as_posix() for p in F11.rglob("*") if p.is_file()}
    assert found == EXPECTED_FILES
    for name in found:
        assert (F11 / name).read_text(encoding="utf-8").strip(), name


def test_f11_answer_key_locations_exist(repo_root):
    key = answer_key("pipeline")
    anchors = json.loads((repo_root / "shared" / "policies" / "severity-rubric.json").read_text(encoding="utf-8"))["anchors"]
    categories = set(json.loads((repo_root / "shared" / "schemas" / "defs.schema.json").read_text(encoding="utf-8"))["$defs"]["category"]["enum"])
    issues = key["issues"]
    assert key["fixture"] == "pipeline" and key["version"] == 1 and key["note"] and "suites" not in key
    assert [issue["id"] for issue in issues] == ["F11-%02d" % n for n in range(1, 8)]
    for issue in issues:
        assert set(issue) == {"id", "location", "file", "lines", "match", "category", "severity_anchor", "class", "detectable_by"}, issue["id"]
        path = F11 / issue["file"]
        assert path.is_file(), issue["id"]
        first, last = issue["lines"]
        lines = lines_of(path)
        assert 1 <= first <= last <= len(lines), issue["id"]
        assert issue["match"] in "\n".join(lines[first - 1:last]), issue["id"]
        assert issue["severity_anchor"] in anchors and issue["category"] == "pipeline" and issue["category"] in categories, issue["id"]
        assert issue["class"] and issue["location"], issue["id"]
        assert issue["detectable_by"] and set(issue["detectable_by"]) <= TOOLS and "static-review" in issue["detectable_by"], issue["id"]
        assert len(set(issue["detectable_by"])) == len(issue["detectable_by"]), issue["id"]
    by_id = {issue["id"]: issue for issue in issues}
    assert [by_id[i]["class"].split(" ")[0] for i in ("F11-01", "F11-02", "F11-03", "F11-05")] == ["CWE-829", "CWE-829", "CWE-94", "CWE-532"]
    assert {i: by_id[i]["detectable_by"] for i in by_id} == {         # measured: actionlint 1.7.12 and zizmor 1.32.0 offline, in their pinned images
        "F11-01": ["zizmor", "static-review"], "F11-02": ["zizmor", "static-review"], "F11-03": ["actionlint", "zizmor", "static-review"],
        "F11-04": ["static-review"], "F11-05": ["static-review"], "F11-06": ["static-review"], "F11-07": ["static-review"]}
    assert len({(issue["file"], tuple(issue["lines"])) for issue in issues}) == 7      # one line of its own per seed


# Where each tool reported a seed (line numbers measured with actionlint 1.7.12 and zizmor 1.32.0 offline): each lies inside the seed's range.
TOOL_LINES = {"F11-01": {"zizmor": 15}, "F11-02": {"zizmor": 3}, "F11-03": {"actionlint": 19, "zizmor": 19}}


def test_f11_every_measured_tool_line_lies_inside_its_seed_range():
    issues = {issue["id"]: issue for issue in answer_key("pipeline")["issues"]}
    for seed, tools in TOOL_LINES.items():
        assert set(tools) <= set(issues[seed]["detectable_by"]), seed
        first, last = issues[seed]["lines"]
        for tool, line in tools.items():
            assert first <= line <= last, (seed, tool)
    for seed, issue in issues.items():
        assert {"actionlint", "zizmor"} & set(issue["detectable_by"]) == set(TOOL_LINES.get(seed, {})), seed


def test_f11_preview_checkout_uses_the_verified_pin():
    pin = [a for a in json.loads((REPO / "shared" / "policies" / "ci-actions.json").read_text(encoding="utf-8"))["actions"] if a["id"] == "checkout"][0]
    assert ("uses: %s@%s" % (pin["uses"], pin["sha"])) in PREVIEW.read_text(encoding="utf-8")


def test_f11_workflows_are_not_at_a_repository_root():
    own = REPO / ".github" / "workflows"
    held = {path.name: path.read_bytes() for path in (CI, PREVIEW)}
    places = []
    for here, directories, _names in os.walk(str(REPO)):
        directories[:] = [d for d in directories if d not in SKIPPED_DIRECTORIES and not d.startswith(".venv") and d != ".tools"]
        if here.endswith(os.sep + ".github") and "workflows" in directories:
            places.append(os.path.join(here, "workflows"))
    for place in places:
        if place == str(own):
            for name, data in held.items():
                assert not (own / name).exists() or (own / name).read_bytes() != data, "an F11 workflow sits at the repository root"
            continue
        assert place == str(WORKFLOWS), place
    assert WORKFLOWS.is_dir()
    assert not (F11 / ".git").exists(), "fixtures/pipeline must not be a repository root"
    assert not (F11 / "workflows").exists()


def test_f11_readme_has_deploy_steps_and_no_rollback():
    text = (F11 / "README.md").read_text(encoding="utf-8")
    deploy = re.search(r"^#+ +Deploy.*?(?=^#+ |\Z)", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    assert deploy, "no deploy section"
    steps = re.findall(r"^\d+\. ", deploy.group(0), re.MULTILINE)
    assert len(steps) >= 4
    headings = re.findall(r"^#+ +(.*)$", text, re.MULTILINE)
    assert headings
    assert not re.search(r"roll[ -]?back|revert|restore|previous (release|version)|downgrade", text, re.IGNORECASE)


def test_f11_workflows_are_block_style_yaml_with_a_minimal_permissions_block():
    for path in (CI, PREVIEW):
        text = path.read_text(encoding="utf-8")
        bare = re.sub(r"\$\{\{.*?\}\}", "EXPR", text)
        assert not re.search(r"[{}\[\]]", bare), path.name                       # no flow mapping or sequence
        assert not re.search(r"(^|\s)[&*][A-Za-z]", bare), path.name             # no anchor or alias
        assert "---" not in text and "<<:" not in text, path.name                # no document marker, no merge key
        assert "\t" not in text, path.name
        assert re.search(r"^permissions:\n  contents: read\n(?!  )", text, re.MULTILINE), path.name
        assert "write" not in text.lower() and "read-all" not in text, path.name


def test_f11_does_not_trigger_the_rules_that_seed_nothing():
    texts = {path.name: lines_of(path) for path in (CI, PREVIEW)}
    for name, lines in texts.items():
        assert not [l for l in lines if "|| true" in l], name
        assert len([l for l in lines if "continue-on-error" in l]) == (1 if name == "ci.yml" else 0), name
        runs = [l for l in lines if l.strip().startswith(("run:", "- run:"))]
        in_run = [l for l in runs if "${{" in l]
        assert len(in_run) == (0 if name == "ci.yml" else 1), name
        assert not [l for l in lines if "secrets." in l and l.strip().startswith(("run:", "- run:"))], name
    unpinned = [l for lines in texts.values() for l in lines if "uses:" in l and not re.search(r"@[0-9a-f]{40}\b", l)]
    assert len(unpinned) == 1 and unpinned[0].strip() == "- uses: actions/checkout@v4"
    secrets = [l for lines in texts.values() for l in lines if "secrets." in l]
    assert secrets and all(re.search(r"\$\{\{ secrets\.[A-Z][A-Z_]+ \}\}$", l) for l in secrets)


def test_f11_files_load_and_deploy_to_nothing_but_reserved_example_domains():
    for path in F11.rglob("*"):
        if path.is_file():
            for url in re.findall(r"https?://[^\s\"'<>`)]+", path.read_text(encoding="utf-8")):
                host = urlparse(url).hostname or ""
                assert host == "example.com" or host.endswith(".example.com"), (path.name, url)
    for path in (CI, PREVIEW, F11 / "README.md", F11 / "Dockerfile"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY", text), path.name


# ----------------------------------------------------------------------------------------------- the qa-release legs
# Each leg's command is read from the ``qa-leg`` blocks of qa-release's references/tools.md, the one text the skill follows, and
# run through the real CLI in a copy of F11 with its own workspace, left untrusted as the skill leaves it: a container run needs
# no trust. No grant is recorded: no leg needs one.

RELEASE = REPO / "skills" / "qa-release"
RELEASE_TOOLS_MD = RELEASE / "references" / "tools.md"
RELEASE_PREFIX = ["python3", "<skill-dir>/scripts/qa.py", "--role", "auditor", "--skill", "qa-release"]
INSTALLED_SCRIPTS = "<skill-dir>/scripts/"
SCAN_RULES = {"F11-01": "unpinned-action", "F11-02": "pr-target-head-checkout", "F11-03": "expression-in-run",
              "F11-04": "tests-allowed-to-fail", "F11-05": "secret-echoed"}
CHECK_RULES = {"F11-06": "rollback-documentation", "F11-07": "base-image-digest"}
# The anchors of references/triage.md: a pipeline finding is rated in the FUNC column.
RULE_ANCHORS = {"unpinned-action": "FUNC-M1", "pr-target-head-checkout": "FUNC-H2", "expression-in-run": "FUNC-H2",
                "tests-allowed-to-fail": "FUNC-M1", "secret-echoed": "FUNC-M1", "rollback-documentation": "FUNC-M2",
                "base-image-digest": "FUNC-L1"}


def release_legs():
    """``{name: (attributes, the command line)}`` from the ``qa-leg`` fenced blocks of qa-release's tools.md."""
    found = {}
    for match in re.finditer(r"^```qa-leg (\S+)((?: [\w-]+=\S+)*)\n(.*?)\n```$", RELEASE_TOOLS_MD.read_text(encoding="utf-8"), re.M | re.S):
        assert match.group(1) not in found, match.group(1)
        found[match.group(1)] = (dict(a.split("=", 1) for a in match.group(2).split()), match.group(3).strip())
    return found


def release_words(name, execution=None):
    """The leg's words after the launcher prefix, with ``<EXE>`` replaced by ``execution``."""
    attributes, line = release_legs()[name]
    words = shlex.split(line.replace("<EXE>", execution) if execution else line)
    assert "<EXE>" not in " ".join(words), name
    assert words[:len(RELEASE_PREFIX)] == RELEASE_PREFIX, words[:len(RELEASE_PREFIX)]
    return attributes, words[len(RELEASE_PREFIX):]


def release_cli(root, words, timeout=900):
    done = subprocess.run([sys.executable, str(LAUNCHER), "--workspace", str(root), "--role", "auditor", "--skill", "qa-release", *words],
                          cwd=str(root), capture_output=True, timeout=timeout)
    return done.returncode, json.loads(done.stdout)


def release_script(root, name, *args):
    done = subprocess.run([sys.executable, "-I", "-B", str(RELEASE / "scripts" / name), *args], cwd=str(root), capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0 and json.loads(done.stdout)["ok"], done.stdout + done.stderr
    return json.loads(done.stdout)


def script_leg(root, name, execution):
    """A host step of tools.md (a script of the skill, not the launcher), run from the skill's own scripts directory."""
    _attributes, line = release_legs()[name]
    words = shlex.split(line.replace("<EXE>", execution))
    assert words[0] == "python3" and words[1].startswith(INSTALLED_SCRIPTS), words[:2]
    return release_script(root, words[1][len(INSTALLED_SCRIPTS):], *words[2:])


@pytest.fixture
def f11_project(qa_on_fixture):
    """A copy of F11 in its own untrusted workspace, with the container engine set; skipped without one."""
    engine = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not engine:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    project = qa_on_fixture("pipeline")
    assert project.cli("config", "set", "container.engine", engine, "--approval", "fixture test")[0] == 0
    code, out = project.cli("config", "set", "project.trust", "untrusted", "--approval", "fixture test")
    assert code == 0 and project.cli("config", "show")[1]["config"]["project"]["trust"] == "untrusted", out
    return project


def seed_finding(item):
    """The finding the skill records for one check result, as references/triage.md's template writes it."""
    path, line = item["at"].rsplit(":", 1)
    return {
        "title": "%s: %s" % (item["at"], item["message"][:120]),
        "description": "%s. Read from the files by the skill's check; the pipeline was not run." % item["message"],
        "category": "pipeline",
        "severity_rationale": {"anchor": RULE_ANCHORS[item["rule"]], "modifiers": []},
        "component": {"path": path, "criticality": "normal"},
        "verification_status": "suspected",
        "evidence": [item["at"]],
        "reproduction_steps": ["Open %s at line %s" % (path, line)],
        "impact": "The pipeline's gate or its integrity is weakened.",
        "probable_cause": "%s %s" % (item["at"], item["rule"]),
        "recommendation": "Change %s in a change a person reviews; nothing was changed here." % item["at"],
        "refs": {"rule": item["rule"], "location": path, "cwe": item["cwe"]},
    }


def record_seeds(project, listed, rules):
    """``{rule: finding id}``: one finding per listed result, recorded with ``finding add`` before any leg."""
    by_rule = {item["rule"]: item for item in listed}
    assert sorted(by_rule) == sorted(rules.values()) and len(listed) == len(rules), listed
    yours = {}
    for rule in sorted(by_rule):
        name = ".qa/work/release/%s.json" % rule
        (project.root / name).write_text(json.dumps(seed_finding(by_rule[rule])), encoding="utf-8")
        code, added = release_cli(project.root, ["finding", "add", "--json", name])
        assert code == 0, added
        yours[rule] = added["id"]
    return yours


def execution_of(project, execution):
    return project.cli("state", "show", "executions")[1]["state"][execution]


def sarif_locations(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    return sorted((r["ruleId"], r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                   r["locations"][0]["physicalLocation"]["region"]["startLine"]) for run in report["runs"] for r in run["results"])


def no_grant_and_untrusted(project):
    assert project.cli("state", "show", "authorizations")[1]["state"] == {}
    assert project.cli("config", "show")[1]["config"]["project"]["trust"] == "untrusted"


def own_check_leg(project, script, leg, rules, sarif):
    issues = {issue["id"]: issue for issue in answer_key("pipeline")["issues"]}
    scan = release_script(project.root, script, "--root", ".", "--out", ".qa/work/release/%s" % sarif)
    yours = record_seeds(project, scan["listed"], rules)
    release_script(project.root, script, "--copy-to-work")
    attributes, words = release_words(leg)
    assert attributes == {"grant": "none", "report": "sarif"}
    assert words[words.index("--container") + 1].startswith("python@sha256:") and "--activity" not in words
    code, out = release_cli(project.root, words)
    print("%s leg:" % leg, out["execution"], "exit", code, out["status"], out.get("reason"), out.get("findings"))
    # Measured on both legs: failed with reason assertion, from the results in the SARIF report.
    assert (code, out["status"], out["reason"]) == (1, "failed", "assertion"), out
    assert out["findings"] == {"opened": 0, "updated": len(rules)}, out
    run = execution_of(project, out["execution"])
    assert run["container"]["network_mode"] == "none" and run["reports"][0]["files"] == ["reports/%s" % sarif]
    found = sarif_locations(project.root / ".qa" / "evidence" / out["execution"] / "reports" / sarif)
    print("%s results:" % leg, found)
    assert found == sorted((rules[i], issues[i]["file"], issues[i]["lines"][0] if i != "F11-02" else 16) for i in rules)
    findings = project.cli("state", "show", "findings")[1]["state"]
    assert sorted(findings) == sorted(yours.values())
    for rule, fid in yours.items():
        assert (findings[fid]["verification_status"], findings[fid]["category"]) == ("suspected", "pipeline"), rule
    no_grant_and_untrusted(project)


@pytest.mark.docker
def test_f11_workflow_scan_leg(f11_project):
    own_check_leg(f11_project, "workflow_scan.py", "workflow-scan", SCAN_RULES, "workflow-scan.sarif")


@pytest.mark.docker
def test_f11_release_check_leg(f11_project):
    own_check_leg(f11_project, "release_check.py", "release-check", CHECK_RULES, "release-check.sarif")


@pytest.mark.docker
def test_f11_actionlint_leg(f11_project):
    """actionlint has no SARIF reporter: its JSON stays in the run's stdout, the skill's converter makes SARIF of it, and the
    ingest attaches it to that run. Its ``expression`` result is a second record of the scan's finding, linked as a duplicate."""
    project = f11_project
    scan = release_script(project.root, "workflow_scan.py", "--root", ".", "--out", ".qa/work/release/workflow-scan.sarif")
    yours = record_seeds(project, scan["listed"], SCAN_RULES)
    attributes, words = release_words("actionlint")
    assert attributes == {"grant": "none", "report": "none"} and "--report" not in words
    assert words[words.index("--container") + 1].startswith("rhysd/actionlint@sha256:") and words[words.index("--activity") + 1] == "static-analysis"
    code, out = release_cli(project.root, words)
    print("actionlint leg:", out["execution"], "exit", code, out["status"], out.get("reason"), out.get("limitations"))
    assert (code, out["status"], out["reason"]) == (1, "failed", "nonzero_exit"), out
    execution = out["execution"]
    run = execution_of(project, execution)
    assert run["container"]["network_mode"] == "none" and run["exit_code"] == 1
    stdout = project.root / ".qa" / "evidence" / execution / "stdout.log"
    print("actionlint stdout:", stdout.read_text(encoding="utf-8")[:1500])
    converted = script_leg(project.root, "actionlint-sarif", execution)
    assert (converted["results"], converted["rules"], converted["out"]) == (1, ["expression"], ".qa/work/release/actionlint.sarif"), converted
    assert sarif_locations(project.root / ".qa" / "work" / "release" / "actionlint.sarif") == [
        ("expression", ".github/workflows/pr-preview.yml", TOOL_LINES["F11-03"]["actionlint"])]
    attributes, words = release_words("actionlint-ingest", execution)
    code, ingested = release_cli(project.root, words)
    print("actionlint ingest:", code, json.dumps(ingested, sort_keys=True)[:1500])
    assert code == 0, ingested
    findings = project.cli("state", "show", "findings")[1]["state"]
    opened = sorted(set(findings) - set(yours.values()))
    assert len(opened) == 1, opened
    other = findings[opened[0]]
    assert (other["category"], other["verification_status"], other["severity_rationale"]["anchor"]) == ("pipeline", "suspected", "TOOL-PROVISIONAL")
    keep = yours["expression-in-run"]
    update = project.root / ".qa" / "work" / "release" / "keep-update.json"
    update.write_text(json.dumps({"evidence": findings[keep]["evidence"] + [execution]}), encoding="utf-8")
    code, updated = release_cli(project.root, ["finding", "update", keep, "--json", ".qa/work/release/keep-update.json"])
    assert code == 0, updated
    code, linked = release_cli(project.root, ["finding", "state", opened[0], "duplicate", "--reason",
                                              "actionlint's expression result at the same line as the scan's expression-in-run finding",
                                              "--duplicate-of", keep])
    assert code == 0, linked
    findings = project.cli("state", "show", "findings")[1]["state"]
    assert findings[opened[0]]["state"] == "duplicate" and execution in findings[keep]["evidence"]
    no_grant_and_untrusted(project)


@pytest.mark.docker
def test_f11_zizmor_leg(f11_project):
    """zizmor offline, its SARIF kept as the run's report: its results are the three seeds the key says it detects, at the lines
    the key measured, each opening a provisional finding beside the scan's (its rule ids are its own)."""
    project = f11_project
    scan = release_script(project.root, "workflow_scan.py", "--root", ".", "--out", ".qa/work/release/workflow-scan.sarif")
    yours = record_seeds(project, scan["listed"], SCAN_RULES)
    attributes, words = release_words("zizmor")
    assert attributes == {"grant": "none", "report": "sarif"} and "--offline" in words[-1]
    assert words[words.index("--container") + 1].startswith("ghcr.io/zizmorcore/zizmor@sha256:")
    code, out = release_cli(project.root, words)
    print("zizmor leg:", out["execution"], "exit", code, out["status"], out.get("reason"), out.get("findings"), out.get("limitations"))
    assert (code, out["status"], out["reason"]) == (1, "failed", "assertion"), out
    run = execution_of(project, out["execution"])
    assert run["container"]["network_mode"] == "none" and run["reports"][0]["files"] == ["reports/zizmor.sarif"]
    found = sarif_locations(project.root / ".qa" / "evidence" / out["execution"] / "reports" / "zizmor.sarif")
    print("zizmor results:", found, "exit code", run["exit_code"])
    issues = {issue["id"]: issue for issue in answer_key("pipeline")["issues"]}
    measured = sorted({(issues[seed]["file"], tools["zizmor"]) for seed, tools in TOOL_LINES.items() if "zizmor" in tools})
    assert sorted({(uri, line) for _rule, uri, line in found}) == measured
    # Measured with zizmor 1.32.0 offline: one result per seed, and exit 0 with --format sarif (the SARIF decides the status).
    assert found == [("zizmor/dangerous-triggers", ".github/workflows/pr-preview.yml", 3),
                     ("zizmor/template-injection", ".github/workflows/pr-preview.yml", 19),
                     ("zizmor/unpinned-uses", ".github/workflows/ci.yml", 15)] and run["exit_code"] == 0
    findings = project.cli("state", "show", "findings")[1]["state"]
    opened = sorted(set(findings) - set(yours.values()))
    assert out["findings"] == {"opened": 3, "updated": 0} and len(opened) == 3, out
    assert {findings[f]["severity_rationale"]["anchor"] for f in opened} == {"TOOL-PROVISIONAL"}
    assert {findings[f]["category"] for f in opened} == {"pipeline"}
    no_grant_and_untrusted(project)
