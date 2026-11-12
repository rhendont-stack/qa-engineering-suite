"""A finding template a skill ships gives only values the kernel's finding schema accepts.

The references give the JSON an agent fills in for ``finding add``. A placeholder such as ``"<high|medium|low>"`` lists the
values it may become; one the schema refuses sends the agent into a kernel refusal and a repair (qa-mutation GREEN run
1112b1f25eb4 hit ``finding_invalid: /component/criticality``).
"""
from __future__ import annotations

import json
import re

import pytest

from helpers import REPO

SCHEMA = REPO / "shared" / "schemas" / "events" / "finding.opened.schema.json"
PLACEHOLDER = re.compile(r'"criticality":\s*"<([^>"]+)>"')
TEXTS = sorted(list(REPO.glob("skills/*/SKILL.md")) + list(REPO.glob("skills/*/references/*.md")))


def allowed():
    return set(json.loads(SCHEMA.read_text())["$defs"]["component"]["properties"]["criticality"]["enum"])


def test_the_scan_sees_the_templates_it_guards():
    found = {path.relative_to(REPO).as_posix() for path in TEXTS if PLACEHOLDER.search(path.read_text())}
    assert {"skills/qa-mutation/references/survivors.md", "skills/qa-regression-guard/references/weakening-patterns.md"} <= found


@pytest.mark.parametrize("path", TEXTS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_every_criticality_placeholder_lists_only_schema_values(path):
    for match in PLACEHOLDER.finditer(path.read_text()):
        values = set(match.group(1).split("|"))
        assert values <= allowed(), "%s: %s not in %s" % (path.name, sorted(values - allowed()), sorted(allowed()))
