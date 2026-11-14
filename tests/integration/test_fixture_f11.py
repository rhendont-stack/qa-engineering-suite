"""F11 ``pipeline``: two GitHub Actions workflows, a Dockerfile and a README, all inert and never pushed anywhere.

The key names each seed by class (CWE or the plain class). The fixture's own files never say what is wrong with them.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from helpers import FIXTURES, REPO, answer_key

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
