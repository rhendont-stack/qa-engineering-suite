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


# --------------------------------------------------------------------------- the environment block (T14-R22)

ENVIRONMENT = {"$ref": "events/execution.recorded.schema.json#/$defs/environment"}
HOST_FACTS = ("cpu_count", "memory_bytes")


def _environment(**changes):
    block = {"class": "local", "platform": "p", "python": "3", "implementation": "CPython", "tools": {},
             "cpu_count": 8, "memory_bytes": 8589934592}
    block.update(changes)
    return block


@pytest.mark.parametrize("field", HOST_FACTS)
@pytest.mark.parametrize("value", [8, None])
def test_the_environment_block_takes_the_host_facts_as_integer_or_null(field, value):
    assert schema.errors(_environment(**{field: value}), ENVIRONMENT) == []


@pytest.mark.parametrize("field", HOST_FACTS)
@pytest.mark.parametrize("value", ["8", 1.5, True, []])
def test_the_environment_block_refuses_anything_else_as_a_host_fact(field, value):
    assert schema.errors(_environment(**{field: value}), ENVIRONMENT) != []


@pytest.mark.parametrize("field", HOST_FACTS)
def test_the_environment_block_requires_each_host_fact(field):
    block = _environment()
    del block[field]
    assert schema.errors(block, ENVIRONMENT) != []


def test_the_environment_block_stays_closed():
    assert schema.errors(_environment(disk_bytes=1), ENVIRONMENT) != []
