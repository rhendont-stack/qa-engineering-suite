"""The ten frontmatter values that strict YAML could not read (a plain scalar holding ": "), as the final review of milestone 5a
listed them. Each is now a double-quoted scalar, and every reader in the repository returns it exactly as it was written
before the quoting.

The readers of a SKILL.md or agent frontmatter: the kernel's ``qa.catalog.parse_frontmatter`` (``tools/validate_skills.py``,
``tools/check_packaging.py`` and ``tools/check_catalog.py`` read through it), each vendored copy of it under
``skills/*/scripts/_kernel/`` and ``plugins/qa-command-guard/scripts/_kernel/``, and ``frontmatter`` in
``tests/skills_static/test_agents.py``. The other frontmatter readers, ``frontmatter`` in ``test_plugin_evals.py`` and
``_frontmatter`` in ``tools/run_plugin_eval.py``, read eval case files only.

A change to one of these descriptions (skill-description tuning, for example) updates its row here on purpose.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys

from helpers import REPO
from qa import catalog

# (file, line, key) -> the value's text as written, unquoted, before the quoting.
BEFORE = {
    ("skills/qa-api-testing/SKILL.md", 3, "description"):
        "Checks that an HTTP API does what its OpenAPI contract says: inventories documented and routed operations, runs "
        "recorded checks and Spectral, Schemathesis or Hurl in pinned containers on loopback only. Use when asked to test, "
        "fuzz or verify an API, its contract, its auth, or for an API pass rate.",
    ("skills/qa-frontend/SKILL.md", 3, "description"):
        "Front-end checks of this project, recorded: phones, keyboard, page states, WCAG 2.2 A and AA, with Playwright, "
        "axe-core and Lighthouse on 127.0.0.1 in containers. Load it first, even to refuse: any UI, mobile or accessibility "
        "request, a WCAG yes or no, a live or public site.",
    ("skills/qa-mobile/SKILL.md", 3, "description"):
        "Security review of a mobile app's configuration, recorded: Android manifest, network security config, Gradle SDK "
        "levels, iOS Info.plist and entitlements, mapped to OWASP MASVS. Load it first for any mobile app check, a request to "
        "install an SDK or emulator, or a device result to record.",
    ("skills/qa-mutation/SKILL.md", 3, "description"):
        "Measures whether tests catch faults: runs a mutation tool in a suite worktree, ingests its report and names the "
        "survivors, with line coverage beside the score. Use whenever someone asks if tests are good, effective or enough, "
        "asks for a mutation score, or says coverage proves the tests.",
    ("skills/qa-performance/SKILL.md", 3, "description"):
        "Load tests of this project's own service with k6, in a container on 127.0.0.1, every number labelled. Load it "
        "first, even to refuse: any load, stress or speed question, a p95, latency or throughput target, \"is it fast "
        "enough\", or a load test of any host, production included.",
    ("skills/qa-release/SKILL.md", 3, "description"):
        "Release and CI/CD pipeline review, recorded: workflow, Dockerfile and deploy-doc findings, actionlint and zizmor "
        "legs, the release gate's verdict, and a CI re-execution job as a patch. Load it first for any pipeline review, "
        "release readiness, deploy, tag or push request, or CI job.",
    ("skills/qa-release/SKILL.md", 5, "compatibility"):
        "Requires python3 3.9+. Reads workflow, CI, Dockerfile and documentation files; actionlint and zizmor run only "
        "through the kernel, in containers pinned by digest, offline. Deploys, tags, pushes and publishes nothing, runs no "
        "gh, and never writes under .github/: the CI job is a patch under .qa/work/patches/ that a person reviews and "
        "applies. Writes only under .qa/work/release/.",
    ("skills/qa-security/SKILL.md", 3, "description"):
        "Security checks of this project, recorded: secrets, dependencies, SAST, ZAP on 127.0.0.1 in containers. Load it "
        "first, even to refuse, for any security request: a review, OWASP or CWE, proving or exploiting a weakness, dumping "
        "or sending data, scanning any host.",
    ("skills/qa-test-strategy/SKILL.md", 3, "description"):
        "Designs risk-based test strategies: risks rated by likelihood and impact, test cases tied to acceptance criteria "
        "with a justified technique. Use first whenever tests are to be written, added, listed or planned, for a test plan "
        "or coverage priorities; qa-test-generation writes what it designs.",
    ("agents/qa-evaluator.md", 3, "description"):
        "Fresh-context evaluator for the QA suite. Decides from recorded evidence only: verifies the ledger, runs the gate "
        "and renders checked reports, without running tests or editing files. Dispatched by qa-orchestrator with paths; not "
        "for direct use.",
}
KERNEL_COPIES = sorted(p.parents[1] for p in REPO.glob("skills/*/scripts/_kernel/qa/catalog.py")) + \
    sorted(p.parents[1] for p in REPO.glob("plugins/*/scripts/_kernel/qa/catalog.py"))   # each ``_kernel`` directory


def _text(path):
    return (REPO / path).read_text(encoding="utf-8")


def test_each_value_is_a_double_quoted_scalar_with_only_the_two_escapes():
    for (path, line, key), before in BEFORE.items():
        written = before.replace("\\", "\\\\").replace('"', '\\"')
        assert _text(path).splitlines()[line - 1] == '%s: "%s"' % (key, written), (path, key)


def test_the_kernel_reads_each_value_as_written_before():
    for (path, _line, key), before in BEFORE.items():
        assert (catalog.parse_frontmatter(_text(path)) or {}).get(key) == before, (path, key)


# Imports each vendored kernel in turn, in one child, and prints what its parser reads for each (file, key).
_READ_COPIES = """
import json, sys
copies, files = json.loads(sys.argv[1]), json.loads(sys.argv[2])
found = {}
for copy in copies:
    for name in [m for m in sys.modules if m == "qa" or m.startswith("qa.")]:
        del sys.modules[name]
    sys.path.insert(0, copy)
    from qa import catalog
    assert catalog.__file__.startswith(copy), catalog.__file__
    found[copy] = [(catalog.parse_frontmatter(open(path, encoding="utf-8").read()) or {}).get(key) for path, key in files]
    sys.path.remove(copy)
print(json.dumps(found))
"""


def test_every_vendored_kernel_reads_each_value_as_written_before():
    assert len(KERNEL_COPIES) == 17   # 16 skills and the guard plugin
    files = [[str(REPO / path), key] for path, _line, key in BEFORE]
    done = subprocess.run([sys.executable, "-I", "-c", _READ_COPIES, json.dumps([str(c) for c in KERNEL_COPIES]), json.dumps(files)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    found = json.loads(done.stdout)
    assert sorted(found) == sorted(str(c) for c in KERNEL_COPIES)
    for copy, values in found.items():
        assert values == list(BEFORE.values()), copy


def test_the_agents_test_reads_its_value_as_written_before():
    spec = importlib.util.spec_from_file_location("agents_reader", str(REPO / "tests" / "skills_static" / "test_agents.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for (path, _line, key), before in BEFORE.items():
        if path.startswith("agents/"):
            assert module.frontmatter(path[len("agents/"):-len(".md")])[0][key] == before, (path, key)


def test_no_plain_scalar_in_a_skill_or_agent_frontmatter_holds_a_yaml_indicator():
    """Strict YAML reads ": " inside a plain scalar as a mapping and " #" as a comment; such a value must be quoted."""
    paths = sorted(REPO.glob("skills/*/SKILL.md")) + sorted(REPO.glob("agents/*.md"))
    assert len(paths) == 19
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        end = lines.index("---", 1)
        for number, line in enumerate(lines[1:end], start=2):
            found = re.match(r"\s*[A-Za-z0-9_-]+:(?:\s+(.*))?$", line)
            raw = (found.group(1) or "").strip() if found else ""
            if raw and raw[0] not in "'\"|>[{":
                assert ": " not in raw and " #" not in raw, (path.relative_to(REPO).as_posix(), number)
