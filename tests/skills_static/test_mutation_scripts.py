"""The bundled scripts of qa-mutation: the mutmut and PIT converters and the static test-smell scan.

The converters turn a tool's own output into mutation-testing-report-schema JSON, the one mutation format the kernel ingests.
Their status tables are fixed from the corpus of the pinned versions (``tests/corpus/producers/mutmut_mutation/captured`` for
mutmut 3.8.1, ``tests/corpus/producers/gradle_pitest/captured`` for PIT 1.30.2): each mutant's converted status is the one the
tool printed for it, and the converted report scores as the tool's own printed counts do. The smell scan is checked per rule
and per language on small files written at test time.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

from helpers import REPO
from qa import ingest

SCRIPTS = REPO / "skills" / "qa-mutation" / "scripts"
PRODUCERS = REPO / "tests" / "corpus" / "producers"
MUTMUT = PRODUCERS / "mutmut_mutation"
PIT = PRODUCERS / "gradle_pitest"
CORPUS = REPO / "tests" / "corpus"


def load(name):
    spec = importlib.util.spec_from_file_location("qa_mutation_" + name, str(SCRIPTS / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(script, *args, cwd):
    done = subprocess.run([sys.executable, "-I", str(SCRIPTS / script)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=120, cwd=str(cwd), env={"PATH": os.environ["PATH"], "HOME": str(cwd)})
    return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


# ----------------------------------------------------------------------------------------------- what the tools printed

def mutmut_printed():
    """``{mutant name: status}`` as ``mutmut results --all true`` printed it, and the counts of ``export-cicd-stats``."""
    text = (MUTMUT / "captured" / "console.txt").read_text(encoding="utf-8")
    results = text.split("=====RESULTS=====", 1)[1].split("=====CICD=====", 1)[0]
    printed = dict(re.findall(r"^\s+(\S+): (.+?)\s*$", results, re.M))
    stats = json.loads(re.search(r"\{.*?\}", text.split("=====CICD=====", 1)[1], re.S).group(0))
    return printed, stats


def mutmut_shown_lines():
    """``{mutant name: the file line its diff changes}`` from ``mutmut show``: the hunk is relative to the function, whose
    first line is its ``def`` line in the source."""
    text = (MUTMUT / "captured" / "console.txt").read_text(encoding="utf-8").split("=====SHOW=====", 1)[1]
    found = {}
    for block in re.split(r"^# ", text, flags=re.M)[1:]:
        name = block.split(":", 1)[0]
        path = re.search(r"^--- (\S+)", block, re.M).group(1)
        hunk = re.search(r"^@@ -(\d+)", block, re.M)
        offset, removed = int(hunk.group(1)), None
        for line in block.split(hunk.group(0), 1)[1].splitlines()[1:]:
            if line.startswith("-"):
                removed = offset
                break
            if not line.startswith("+"):
                offset += 1
        function = re.search(r"x_(\w+?)__mutmut_", name).group(1)
        source = (MUTMUT / path).read_text(encoding="utf-8").splitlines()
        def_line = next(n for n, l in enumerate(source, 1) if l.lstrip().startswith("def %s(" % function))
        found[name] = def_line + removed - 1
    return found


def pit_printed():
    """PIT's own totals: ``{status: count}`` summed over its per-mutator lines, and the statistics block's numbers."""
    text = (PIT / "captured" / "console.txt").read_text(encoding="utf-8")
    counts = {}
    for status, n in re.findall(r"\b([A-Z_]+) (\d+)\b", text.split("- Timings", 1)[0]):
        counts[status] = counts.get(status, 0) + int(n)
    generated = re.search(r">> Generated (\d+) mutations Killed (\d+) \((\d+)%\)", text)
    strength = re.search(r"Mutations with no coverage (\d+)\. Test strength (\d+)%", text)
    return counts, {"generated": int(generated.group(1)), "killed": int(generated.group(2)), "percent": int(generated.group(3)),
                    "no_coverage": int(strength.group(1)), "strength": int(strength.group(2))}


def convert_mutmut(tmp_path):
    out = tmp_path / "mutmut.json"
    code, body, err = run("mutmut_to_report.py", "--root", MUTMUT, "--mutants", MUTMUT / "captured" / "mutants", "--out", out,
                          cwd=tmp_path)
    assert code == 0, (body, err)
    return json.loads(out.read_text(encoding="utf-8")), body, out


def convert_pit(tmp_path):
    out = tmp_path / "pit.json"
    code, body, err = run("pit_to_report.py", "--xml", PIT / "captured" / "mutations.xml", "--source-root", "src/main/java",
                          "--out", out, cwd=PIT)
    assert code == 0, (body, err)
    return json.loads(out.read_text(encoding="utf-8")), body, out


def statuses(doc):
    return {m["id"]: m["status"] for f in doc["files"].values() for m in f["mutants"]}


# ----------------------------------------------------------------------------------------------- mutmut

MUTMUT_SCHEMA = {"killed": "Killed", "survived": "Survived", "no tests": "NoCoverage", "timeout": "Timeout",
                 "caught by type check": "CompileError", "segfault": "RuntimeError", "suspicious": "RuntimeError",
                 "skipped": "Ignored", "check was interrupted by user": "Pending", "not checked": "Pending"}


def test_mutmut_statuses_map_to_the_schema(tmp_path):
    convert = load("mutmut_to_report")
    printed, _stats = mutmut_printed()
    doc, _body, _out = convert_mutmut(tmp_path)
    got = statuses(doc)
    assert set(got) == set(printed) and len(got) == 26
    for name, status in printed.items():
        assert got[name] == MUTMUT_SCHEMA[status], name
    # mutmut 3.8.1's own exit-code table (mutmut/stats.py), every entry, and its default for any other code
    table = {1: "killed", 3: "killed", 0: "survived", 5: "no tests", 2: "check was interrupted by user", None: "not checked",
             33: "no tests", 34: "skipped", 35: "suspicious", 36: "timeout", 37: "caught by type check", -24: "timeout",
             24: "timeout", 152: "timeout", 255: "timeout", -11: "segfault", -9: "segfault"}
    for code, status in table.items():
        assert convert.mutmut_status(code) == status
        assert convert.schema_status(code) == MUTMUT_SCHEMA[status]
    assert convert.mutmut_status(77) == "suspicious" and convert.schema_status(77) == "RuntimeError"


def test_mutmut_mutants_are_placed_on_the_line_mutmut_shows(tmp_path):
    doc, _body, _out = convert_mutmut(tmp_path)
    shown = mutmut_shown_lines()
    placed = {m["id"]: m["location"]["start"]["line"] for f in doc["files"].values() for m in f["mutants"]}
    assert shown and {name: placed[name] for name in shown} == shown
    survivor = next(m for m in doc["files"]["shipping/cost.py"]["mutants"] if m["id"].endswith("__mutmut_9"))
    assert survivor["status"] == "Survived" and survivor["location"]["start"]["line"] == 5
    assert ">=" in survivor["replacement"] and survivor["description"].startswith("if weight_kg > 10:")
    assert sorted(doc["files"]) == ["shipping/cost.py", "shipping/label.py"]  # a file with no mutant is left out
    assert doc["files"]["shipping/cost.py"]["source"] == (MUTMUT / "shipping" / "cost.py").read_text(encoding="utf-8")


def test_mutmut_converter_refuses_a_directory_without_mutation_data(tmp_path):
    (tmp_path / "mutants").mkdir()
    code, body, _err = run("mutmut_to_report.py", "--root", tmp_path, "--out", tmp_path / "x.json", cwd=tmp_path)
    assert code == 2 and body["ok"] is False and body["error"]["code"] == "no_mutation_data"
    assert not (tmp_path / "x.json").exists()


# ----------------------------------------------------------------------------------------------- PIT

PIT_SCHEMA = {"KILLED": "Killed", "SURVIVED": "Survived", "NO_COVERAGE": "NoCoverage", "TIMED_OUT": "Timeout",
              "NON_VIABLE": "CompileError", "MEMORY_ERROR": "RuntimeError", "RUN_ERROR": "RuntimeError", "NOT_STARTED": "Pending",
              "STARTED": "Pending", "EQUIVALENT": "Ignored"}


def test_pit_statuses_map_to_the_schema(tmp_path):
    convert = load("pit_to_report")
    counts, _stats = pit_printed()
    doc, _body, _out = convert_pit(tmp_path)
    got = {}
    for status in statuses(doc).values():
        got[status] = got.get(status, 0) + 1
    want = {}
    for status, n in counts.items():
        if n:
            want[PIT_SCHEMA[status]] = want.get(PIT_SCHEMA[status], 0) + n
    assert got == want == {"Killed": 9, "Survived": 1, "NoCoverage": 3}
    xml = (PIT / "captured" / "mutations.xml").read_text(encoding="utf-8")
    in_order = sorted((m for f in doc["files"].values() for m in f["mutants"]), key=lambda m: int(m["id"]))  # ids follow PIT's order
    assert [m["status"] for m in in_order] == [PIT_SCHEMA[s] for s in re.findall(r"status='(\w+)'", xml)]
    assert set(counts) <= set(PIT_SCHEMA) and convert.STATUS == PIT_SCHEMA
    survivor = next(m for f in doc["files"].values() for m in f["mutants"] if m["status"] == "Survived")
    assert survivor["location"]["start"]["line"] == 12 and survivor["mutatorName"] == "ConditionalsBoundaryMutator"
    assert sorted(doc["files"]) == ["src/main/java/corpus/Label.java", "src/main/java/corpus/Shipping.java"]
    killed = next(m for f in doc["files"].values() for m in f["mutants"] if m["status"] == "Killed")
    assert killed["killedBy"] and all("ShippingTest" in t for t in killed["killedBy"])


@pytest.mark.parametrize("body, code", [
    (b"<?xml version='1.0'?><!DOCTYPE m [<!ENTITY a 'x'>]><mutations>&a;</mutations>", "report_invalid"),
    (b"<mutations><mutation status='EXPLODED'><sourceFile>A.java</sourceFile><mutatedClass>A</mutatedClass>"
     b"<lineNumber>1</lineNumber><mutator>M</mutator></mutation></mutations>", "unknown_status"),
    (b"<mutations><mutation status='KILLED'><sourceFile>A.java</sourceFile><mutatedClass>A</mutatedClass>"
     b"<lineNumber>0</lineNumber><mutator>M</mutator></mutation></mutations>", "report_invalid"),
    (b"<testsuite/>", "report_invalid"),
])
def test_pit_converter_refuses_hostile_or_foreign_input(tmp_path, body, code):
    (tmp_path / "m.xml").write_bytes(body)
    rc, out, _err = run("pit_to_report.py", "--xml", tmp_path / "m.xml", "--out", tmp_path / "x.json", cwd=tmp_path)
    assert rc == 2 and out["ok"] is False and out["error"]["code"] == code
    assert not (tmp_path / "x.json").exists()


# ----------------------------------------------------------------------------------------------- both

MUTANT_KEYS = {"id", "mutatorName", "location", "status", "replacement", "description", "killedBy", "statusReason"}


def check_schema_shape(doc):
    """The required properties of mutation-testing-report-schema (``schemaVersion``, ``thresholds.high``/``low``, ``files``
    with ``language``, ``source`` and ``mutants``; each mutant's ``id``, ``mutatorName``, ``location.start``/``end`` with
    ``line`` and ``column`` from 1, and a status of the schema's eight), and no property the schema does not define."""
    assert set(doc) <= {"schemaVersion", "thresholds", "files", "config", "projectRoot", "framework"}
    assert isinstance(doc["schemaVersion"], str)
    assert set(doc["thresholds"]) == {"high", "low"} and 0 <= doc["thresholds"]["low"] <= doc["thresholds"]["high"] <= 100
    for entry in doc["files"].values():
        assert set(entry) == {"language", "source", "mutants"} and isinstance(entry["source"], str)
        for m in entry["mutants"]:
            assert {"id", "mutatorName", "location", "status"} <= set(m) <= MUTANT_KEYS
            assert m["status"] in ingest.mutation.STATUSES and isinstance(m["id"], str) and m["mutatorName"]
            for side in ("start", "end"):
                pos = m["location"][side]
                assert set(pos) == {"line", "column"} and pos["line"] >= 1 and pos["column"] >= 1
            assert (m["location"]["end"]["line"], m["location"]["end"]["column"]) >= (m["location"]["start"]["line"],
                                                                                       m["location"]["start"]["column"])


def test_converted_report_validates_and_scores_like_the_tool(tmp_path):
    for doc, body, out, printed in ((convert_mutmut(tmp_path) + (mutmut_printed()[1],)), (convert_pit(tmp_path) + (pit_printed()[1],))):
        check_schema_shape(doc)
        summary = ingest.parse_report("mutation", out, 50 * 2**20)  # the kernel's own parser accepts it
        assert summary.kind == "mutants" and summary.anomalies == ()
        score = ingest.mutation.score(summary.mutants)
        assert body["ok"] is True and body["mutants"] == len(summary.mutants) and body["score"] == pytest.approx(score)
        per_file = {}                       # the per-file scores it prints are the kernel's over each file's mutants
        for m in summary.mutants:
            per_file.setdefault(m.file, []).append(m)
        assert sorted(body["by_file"]) == sorted(per_file)
        for path, mutants in per_file.items():
            assert body["by_file"][path]["score"] == ingest.mutation.score(mutants)
            assert sum(body["by_file"][path]["statuses"].values()) == len(mutants)
        if "generated" in printed:          # PIT prints its score, as a whole percent, and its test strength
            assert len(summary.mutants) == printed["generated"] and round(score) == printed["percent"]
            covered = ingest.mutation.score([m for m in summary.mutants if m.status != "NoCoverage"])
            assert round(covered) == printed["strength"]
        else:                               # mutmut prints counts only; the schema's formula over its own counts
            assert len(summary.mutants) == printed["total"]
            tool = 100.0 * (printed["killed"] + printed["timeout"]) / (
                printed["killed"] + printed["timeout"] + printed["survived"] + printed["no_tests"])
            assert score == pytest.approx(tool)
    # the committed corpus files are exactly what the converters write from the capture
    assert (CORPUS / "mutation" / "mutmut-3.8.1-shipping.json").read_bytes() == (tmp_path / "mutmut.json").read_bytes()
    assert (CORPUS / "mutation" / "pit-1.30.2-shipping.json").read_bytes() == (tmp_path / "pit.json").read_bytes()


def test_converters_are_deterministic(tmp_path):
    a, _b, first = convert_mutmut(tmp_path)
    data = first.read_bytes()
    convert_mutmut(tmp_path)
    assert first.read_bytes() == data and data.endswith(b"\n")


# ----------------------------------------------------------------------------------------------- the smell scan

def smells(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    code, body, err = run("test_smells.py", ".", cwd=tmp_path)
    assert code == 0, err
    return body


def rules_of(body):
    return sorted((s["rule"], s["path"], s["test"]) for s in body["smells"])


def test_python_smells_each_rule(tmp_path):
    body = smells(tmp_path, {"tests/test_x.py": """
        import time
        import pytest
        from app import calc


        def test_runs_without_checking():
            calc.total(3)


        def test_compares_a_call_with_itself():
            assert calc.total(3) == calc.total(3)


        def test_mocks_what_it_tests(monkeypatch):
            monkeypatch.setattr(calc, "total", lambda *a: 9)
            assert calc.total(3) == 9


        def test_waits_for_it():
            calc.start()
            time.sleep(0.5)
            assert calc.done()


        def test_swallows_the_failure():
            try:
                assert calc.total(3) == 4
            except Exception:
                pass


        def test_only_a_snapshot(snapshot):
            snapshot.assert_match(calc.render(), "render.txt")


        def test_real_check():
            with pytest.raises(ValueError):
                calc.total(-1)
            assert calc.total(2) == 4


        class TestThing:
            def test_constant(self):
                assert True
    """})
    assert rules_of(body) == sorted([
        ("assertion-free", "tests/test_x.py", "test_runs_without_checking"),
        ("tautological", "tests/test_x.py", "test_compares_a_call_with_itself"),
        ("over-mocked", "tests/test_x.py", "test_mocks_what_it_tests"),
        ("sleep-based", "tests/test_x.py", "test_waits_for_it"),
        ("catch-all", "tests/test_x.py", "test_swallows_the_failure"),
        ("snapshot-only", "tests/test_x.py", "test_only_a_snapshot"),
        ("tautological", "tests/test_x.py", "TestThing::test_constant"),
    ])
    line = {s["test"]: s["line"] for s in body["smells"]}
    assert line["test_compares_a_call_with_itself"] == 12 and line["test_mocks_what_it_tests"] == 16
    assert body["tests"] == 8 and body["files_scanned"] == 1


def test_python_mocks_counted_and_unittest_assertions_seen(tmp_path):
    body = smells(tmp_path, {"tests/test_y.py": """
        import unittest
        from unittest import mock


        class T(unittest.TestCase):
            def test_uses_self_assert(self):
                self.assertEqual(1 + 1, 2)

            def test_same_both_sides(self):
                self.assertEqual(len("ab"), len("ab"))

            @mock.patch("app.a")
            @mock.patch("app.b")
            @mock.patch("app.c")
            @mock.patch("app.d")
            def test_four_patches(self, a, b, c, d):
                self.assertTrue(a.called)
    """})
    assert rules_of(body) == [("over-mocked", "tests/test_y.py", "T::test_four_patches"),
                              ("tautological", "tests/test_y.py", "T::test_same_both_sides")]


def test_javascript_and_java_smells(tmp_path):
    body = smells(tmp_path, {
        "web/test/a.test.js": """
            import { expect, it, vi } from "vitest";
            import { fmt } from "../src/a.js";

            it("formats", () => {
              fmt(3);
            });

            it("is itself", () => {
              expect(fmt(3)).toBe(fmt(3));
            });

            it("snapshots", () => {
              expect(fmt(3)).toMatchSnapshot();
            });

            it("waits", async () => {
              await new Promise((r) => setTimeout(r, 200));
              expect(fmt(3)).toBe("3");
            });

            it("checks", () => {
              expect(fmt(3)).toBe("3.00");
            });
        """,
        "jvm/src/test/java/x/ATest.java": """
            package x;

            import static org.junit.jupiter.api.Assertions.assertEquals;
            import org.junit.jupiter.api.Test;

            class ATest {
                @Test
                void runsOnly() {
                    new A().run(4);
                }

                @Test
                void same() {
                    assertEquals(new A().get(), new A().get());
                }

                @Test
                void swallows() {
                    try {
                        assertEquals(1, new A().get());
                    } catch (Throwable t) {
                    }
                }

                @Test
                void sleeps() throws Exception {
                    Thread.sleep(100);
                    assertEquals(1, new A().get());
                }

                @Test
                void good() {
                    assertEquals(1, new A().get());
                }
            }
        """})
    assert rules_of(body) == sorted([
        ("assertion-free", "web/test/a.test.js", "formats"),
        ("tautological", "web/test/a.test.js", "is itself"),
        ("snapshot-only", "web/test/a.test.js", "snapshots"),
        ("sleep-based", "web/test/a.test.js", "waits"),
        ("assertion-free", "jvm/src/test/java/x/ATest.java", "runsOnly"),
        ("tautological", "jvm/src/test/java/x/ATest.java", "same"),
        ("catch-all", "jvm/src/test/java/x/ATest.java", "swallows"),
        ("sleep-based", "jvm/src/test/java/x/ATest.java", "sleeps"),
    ])


def test_smell_scan_skips_what_is_not_the_projects_and_is_deterministic(tmp_path):
    files = {"tests/test_ok.py": "def test_a():\n    x = 1\n", ".qa/work/wt-1/tests/test_ok.py": "def test_a():\n    pass\n",
             "mutants/tests/test_ok.py": "def test_a():\n    pass\n", "node_modules/p/x.test.js": "it('a', () => {});\n",
             "src/app.py": "def test_helper():\n    pass\n"}
    first = smells(tmp_path, files)
    assert [(s["path"], s["test"]) for s in first["smells"]] == [("tests/test_ok.py", "test_a")]
    code, second, _err = run("test_smells.py", ".", cwd=tmp_path)
    assert code == 0 and second == first
    code, only, _err = run("test_smells.py", "tests/test_ok.py", cwd=tmp_path)
    assert code == 0 and only["files_scanned"] == 1
