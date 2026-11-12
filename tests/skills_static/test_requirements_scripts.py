"""The bundled scanner of qa-requirements: weak words in English and Spanish, deterministic and cited by line and column."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from helpers import FIXTURES, REPO, answer_key

SCRIPTS = REPO / "skills" / "qa-requirements" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import weak_words  # noqa: E402


def run(*args, cwd=None):
    done = subprocess.run([sys.executable, "-I", str(SCRIPTS / "weak_words.py")] + [str(a) for a in args], capture_output=True,
                          text=True, timeout=60, cwd=cwd)
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stdout


def found(hits):
    return {(h["line"], h["matched"].lower()) for h in hits}


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------------------ the F5 requirements

def test_the_f5_requirements_flag_the_english_and_spanish_words_on_their_lines():
    code, out, _ = run(FIXTURES / "seeded-defects" / "requirements.md")
    assert code == 0 and out["ok"] is True
    hits = found(out["hits"])
    expected = {(11, "quickly"), (11, "should"), (11, "easy"), (13, "appropriate"), (13, "as needed"), (13, "and so on"), (13, "etc."),
                (29, "rápida"), (29, "amigable"), (31, "cuando sea posible"), (31, "según sea necesario")}
    assert expected <= hits
    flagged_lines = {line for line, _ in hits}
    assert not flagged_lines & {7, 9, 15, 19, 23, 25}  # requirements with no weak word stay quiet


def test_the_answer_key_words_are_all_found():
    key = {issue["id"]: issue for issue in answer_key("seeded-defects")["issues"]}
    code, out, _ = run(FIXTURES / "seeded-defects" / "requirements.md")
    matched = " | ".join(sorted(h["matched"].lower() for h in out["hits"]))
    for issue in ("F5-06", "F5-07"):
        words = key[issue]["expected_record"].split(":", 1)[1]
        for word in (w.strip().rstrip(".") for w in words.split(",")):
            assert word.lower() in matched, (issue, word)


# ------------------------------------------------------------------------------------------------ matching

@pytest.mark.parametrize("text", ["La respuesta es rápida.", "La respuesta es RAPIDA.", "La respuesta es Rapida.",
                                  "La respuesta es Rápida."])
def test_spanish_words_match_with_or_without_accents_in_any_case(text):
    hits = weak_words.scan_text(text)
    assert [(h["term"], h["line"], h["column"]) for h in hits] == [("rápida", 1, 17)]


def test_a_phrase_wins_over_the_words_inside_it():
    hits = weak_words.scan_text("Pages load as fast as possible and the form is user-friendly.")
    assert [h["term"] for h in hits] == ["as fast as possible", "user-friendly"]


def test_a_phrase_may_span_several_spaces_but_not_a_line_break():
    assert [h["term"] for h in weak_words.scan_text("rounded as   needed")] == ["as needed"]
    assert weak_words.scan_text("rounded as\nneeded") == []


@pytest.mark.parametrize("text", ["Serve breakfast at eight.", "The user felt uneasy.", "We fetch the order.", "The shoulder of the road.",
                                  "Una situación anormalmente larga."])
def test_a_term_inside_another_word_is_not_flagged(text):
    assert weak_words.scan_text(text) == []


def test_columns_are_one_based_and_count_characters_not_bytes():
    hits = weak_words.scan_text("Línea uno\nEl cálculo debería ser rápido.")
    assert [(h["term"], h["line"], h["column"]) for h in hits] == [("debería", 2, 12), ("rápido", 2, 24)]
    assert [h["matched"] for h in hits] == ["debería", "rápido"]


def test_each_hit_names_its_language_and_kind():
    hits = weak_words.scan_text("It should be flexible, etc.")
    by_term = {h["term"]: h for h in hits}
    assert by_term["should"]["langs"] == ["en"] and by_term["should"]["kind"] == "weak-modal"
    assert by_term["flexible"]["langs"] == ["en", "es"]  # the same word in both lists is one hit
    assert by_term["etc."]["kind"] == "open-ended"
    assert all(h["kind"] in weak_words.KINDS for h in hits)


def test_the_language_option_limits_the_lists():
    text = "It should be fast. Debe ser rápido. Es flexible."
    assert {h["term"] for h in weak_words.scan_text(text, lang="en")} == {"should", "fast", "flexible"}
    assert {h["term"] for h in weak_words.scan_text(text, lang="es")} == {"rápido", "flexible"}
    assert {h["term"] for h in weak_words.scan_text(text, lang="all")} == {"should", "fast", "rápido", "flexible"}


def test_every_listed_term_finds_itself_once():
    for term, langs, kind in weak_words.terms():
        hits = weak_words.scan_text("x %s y" % term)
        assert [(h["term"], h["column"]) for h in hits] == [(term, 3)], term
        assert hits[0]["langs"] == langs and hits[0]["kind"] == kind


def test_both_lists_hold_the_documented_examples():
    terms = {term for term, _, _ in weak_words.terms()}
    assert {"fast", "user-friendly", "etc.", "as appropriate", "should"} <= terms
    assert {"rápido", "intuitivo", "según corresponda"} <= terms


# ------------------------------------------------------------------------------------------------ the command

def test_the_output_is_sorted_and_the_same_bytes_on_every_run(tmp_path):
    first = write(tmp_path, "b.md", "R1. It must be fast.\n\nR2. Etc.\n")
    second = write(tmp_path, "a.md", "R1. Debe ser intuitivo y rápido.\n")
    code, out, raw = run("b.md", "a.md", cwd=tmp_path)
    assert code == 0
    assert [(h["path"], h["line"], h["column"]) for h in out["hits"]] == [("b.md", 1, 16), ("b.md", 3, 5), ("a.md", 1, 14), ("a.md", 1, 26)]
    assert out["files"] == ["b.md", "a.md"] and out["counts"] == {"total": 4, "by_file": {"a.md": 2, "b.md": 2}, "by_kind": {"vague": 3, "open-ended": 1}}
    assert run("b.md", "a.md", cwd=tmp_path)[2] == raw
    assert first.read_text(encoding="utf-8").startswith("R1") and second.is_file()  # the scanner writes nothing


def test_a_file_with_no_weak_word_reports_none(tmp_path):
    write(tmp_path, "r.md", "R1. An order of 100.00 or more receives a 10 percent discount.\n")
    code, out, _ = run("r.md", cwd=tmp_path)
    assert code == 0 and out["hits"] == [] and out["counts"]["total"] == 0


def test_a_byte_order_mark_does_not_shift_the_first_line(tmp_path):
    (tmp_path / "bom.md").write_bytes(b"\xef\xbb\xbfIt is fast.\n")
    code, out, _ = run("bom.md", cwd=tmp_path)
    assert code == 0 and [(h["line"], h["column"]) for h in out["hits"]] == [(1, 7)]


@pytest.mark.parametrize("kind", ["missing", "directory", "not_utf8"])
def test_an_unreadable_input_exits_2_with_an_error_object(tmp_path, kind):
    if kind == "directory":
        (tmp_path / "docs").mkdir()
        name = "docs"
    elif kind == "not_utf8":
        (tmp_path / "latin1.md").write_bytes("rápido".encode("latin-1"))
        name = "latin1.md"
    else:
        name = "nothing.md"
    code, out, _ = run(name, cwd=tmp_path)
    assert code == 2
    assert out["ok"] is False and out["error"]["path"] == name and out["error"]["code"] in ("file_unreadable", "not_utf8")


def test_no_file_is_a_usage_error(tmp_path):
    code, out, _ = run(cwd=tmp_path)
    assert code == 2 and out["ok"] is False and out["error"]["code"] == "usage"
