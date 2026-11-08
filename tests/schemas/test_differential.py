from __future__ import annotations

import json
from pathlib import Path

import pytest

from qa import schema

jsonschema = pytest.importorskip("jsonschema")

CASES = json.loads((Path(__file__).parent / "differential_cases.json").read_text(encoding="utf-8"))


def test_cases_cover_every_supported_keyword():
    used = set()

    def walk(node):
        if isinstance(node, dict):
            used.update(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for case in CASES:
        walk(case["schema"])
    assert schema.SUPPORTED_KEYWORDS - used == set()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_matches_jsonschema(case):
    validator = jsonschema.Draft202012Validator(case["schema"])
    assert case["valid"] or case["invalid"]
    for kind, instances in (("valid", case["valid"]), ("invalid", case["invalid"])):
        for instance in instances:
            ours = bool(schema.errors(instance, case["schema"]))
            theirs = not validator.is_valid(instance)
            assert ours == theirs, (case["name"], kind, instance)
            assert ours == (kind == "invalid"), (case["name"], kind, instance)
