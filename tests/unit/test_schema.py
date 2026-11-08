from __future__ import annotations

import pytest

from qa import schema
from qa.errors import QaError

SHA = "defs.schema.json#/$defs/sha256"


def test_bool_is_not_integer_and_one_point_zero_is():
    assert schema.errors(True, {"type": "integer"})
    assert not schema.errors(1.0, {"type": "integer"})


def test_pattern_is_unanchored_search():
    assert not schema.errors("xabcx", {"pattern": "abc"})


def test_unique_items_distinguishes_bool_and_int():
    assert not schema.errors([1, True], {"uniqueItems": True})
    assert schema.errors([1, 1.0], {"uniqueItems": True})


def test_format_is_annotation_only():
    assert not schema.errors("not-a-date", {"format": "date-time"})


def test_unknown_keyword_raises():
    with pytest.raises(schema.SchemaError):
        schema.errors(1, {"unevaluatedProperties": False})


def test_messages_carry_json_pointer():
    errs = schema.errors({"a": [1, "x"]}, {"properties": {"a": {"items": {"type": "integer"}}}})
    assert errs and errs[0].startswith("/a/1")


def test_cross_file_ref():
    s = {"$ref": SHA}
    assert not schema.errors("sha256:" + "0" * 64, s) and schema.errors("sha256:xyz", s)


def test_validate_raises_schema_invalid():
    with pytest.raises(QaError) as e:
        schema.validate("nope", "defs.schema.json#/$defs/status")
    assert e.value.code == "schema_invalid"
    assert e.value.details["errors"]


def test_validate_accepts_valid_instance():
    schema.validate("passed", "defs.schema.json#/$defs/status")


def test_schema_error_is_qa_error():
    assert issubclass(schema.SchemaError, QaError)


def test_trailing_newline_does_not_match_dollar():
    assert schema.errors("sha256:" + "0" * 64 + "\n", {"$ref": SHA})
    assert schema.errors("ab\n", {"pattern": "^ab$"})
    assert not schema.errors("ab", {"pattern": "^ab$"})


def test_dollar_inside_class_and_escaped_dollar_are_literal():
    assert not schema.errors("a$", {"pattern": "^a[$]\\Z"})
    assert not schema.errors("a$", {"pattern": "^a\\$$"})
    assert schema.errors("a", {"pattern": "^a\\$$"})


def test_root_pointer_is_empty():
    assert schema.errors(1, {"type": "string"})[0].startswith(": ")


def test_pointer_escapes_tilde_and_slash():
    errs = schema.errors({"a/b~c": 1}, {"properties": {"a/b~c": {"type": "string"}}})
    assert errs[0].startswith("/a~1b~0c: ")


def test_errors_order_is_deterministic():
    s = {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["c"]}
    i = {"a": 1, "b": 2}
    assert schema.errors(i, s) == schema.errors(i, s)


def test_in_document_ref():
    s = {"$defs": {"n": {"type": "integer"}}, "properties": {"a": {"$ref": "#/$defs/n"}}}
    assert not schema.errors({"a": 1}, s) and schema.errors({"a": "x"}, s)


def test_base_resolves_fragments_against_file():
    s = {"$ref": "#/$defs/status"}
    assert not schema.errors("passed", s, base="defs.schema.json")
    assert schema.errors("nope", s, base="defs.schema.json")


def test_actor_def():
    ok = {"role": "verifier", "skill": None, "session": "S-" + "0" * 26}
    assert not schema.errors(ok, {"$ref": "defs.schema.json#/$defs/actor"})
    bad = dict(ok, independence="I1")
    assert schema.errors(bad, {"$ref": "defs.schema.json#/$defs/actor"})
    del ok["skill"]
    assert schema.errors(ok, {"$ref": "defs.schema.json#/$defs/actor"})


@pytest.mark.parametrize("ref", [
    "other.json#/$defs/x",
    "defs.schema.json#/definitions/x",
    "defs.schema.json",
    "#/$defs/missing",
    "defs.schema.json#/$defs/missing",
    "nofile.schema.json#/$defs/x",
    "../defs.schema.json#/$defs/x",
    "/etc/defs.schema.json#/$defs/x",
    "https://example.com/x.schema.json#/$defs/x",
])
def test_bad_refs_raise(ref):
    with pytest.raises(schema.SchemaError):
        schema.errors("x", {"$ref": ref})


def test_ref_cycle_raises():
    s = {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}
    with pytest.raises(schema.SchemaError) as e:
        schema.errors(1, s)
    assert e.value.code == "ref_cycle"


def test_recursive_schema_terminates():
    s = {"$defs": {"t": {"type": "object", "properties": {"c": {"$ref": "#/$defs/t"}}}}, "$ref": "#/$defs/t"}
    assert not schema.errors({"c": {"c": {}}}, s)
    assert schema.errors({"c": {"c": 1}}, s)


def test_static_check_covers_unreached_branches():
    with pytest.raises(schema.SchemaError):
        schema.errors(1, {"if": {"type": "string"}, "then": {"unevaluatedItems": False}})
    with pytest.raises(schema.SchemaError):
        schema.errors(1, {"$defs": {"x": {"prefixItems": []}}})


def test_data_keywords_are_not_walked():
    data = {"unevaluatedProperties": 1}
    s = {"enum": [data], "default": data, "examples": [data]}
    assert not schema.errors(data, s)
    assert not schema.errors(data, {"const": data})


def test_property_named_like_keyword_is_fine():
    s = {"properties": {"unevaluatedProperties": {"type": "integer"}}}
    assert not schema.errors({"unevaluatedProperties": 1}, s)


def test_boolean_subschemas():
    assert not schema.errors(1, {"properties": {"a": True}, "additionalProperties": False})
    assert schema.errors({"a": 1}, {"additionalProperties": False})
    assert schema.errors(1, False) and not schema.errors(1, True)


def test_bad_pattern_raises_schema_error():
    with pytest.raises(schema.SchemaError):
        schema.errors("x", {"pattern": "("})


def test_load_schema_is_cached_and_checked():
    a = schema.load_schema("defs.schema.json")
    assert schema.load_schema("defs.schema.json") is a
    with pytest.raises(schema.SchemaError):
        schema.load_schema("missing.schema.json")
    with pytest.raises(schema.SchemaError):
        schema.load_schema("../pytest.ini")


def test_supported_keywords_contents():
    assert "unevaluatedProperties" not in schema.SUPPORTED_KEYWORDS
    assert {"$ref", "dependentRequired", "multipleOf", "if", "format"} <= schema.SUPPORTED_KEYWORDS
    assert len(schema.SUPPORTED_KEYWORDS) == 41


def test_defs_vocabularies():
    d = schema.load_schema("defs.schema.json")["$defs"]
    assert len(d["reason"]["enum"]) == 17 and "skipped" in d["reason"]["enum"]
    assert len(d["activity"]["enum"]) == 17
    assert len(d["category"]["enum"]) == 12
    assert len(d["finding_state"]["enum"]) == 7
    assert d["severity"]["enum"][1] == "HIGH"
    assert schema.errors("EV-" + "a" * 12, {"$ref": "defs.schema.json#/$defs/ev_id"}) == []
