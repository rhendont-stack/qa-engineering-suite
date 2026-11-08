from __future__ import annotations

import json

import pytest

from qa import resource_dir, schema

FILES = sorted(p.relative_to(resource_dir("schemas")).as_posix()
               for p in resource_dir("schemas").rglob("*.schema.json"))


def test_defs_schema_is_shipped():
    assert "defs.schema.json" in FILES


@pytest.mark.parametrize("name", FILES)
def test_uses_only_supported_keywords(name):
    schema.load_schema(name)  # raises SchemaError on an unsupported keyword or a bad $ref


@pytest.mark.parametrize("name", FILES)
def test_header_is_draft_2020_12_without_invented_url(name):
    doc = schema.load_schema(name)
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc.get("$id", name.rsplit("/", 1)[-1]) == name.rsplit("/", 1)[-1]


@pytest.mark.parametrize("name", FILES)
def test_conforms_to_metaschema(name):
    jsonschema = pytest.importorskip("jsonschema")
    path = resource_dir("schemas") / name
    jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
