"""The ``claude plugin eval`` cases of every skill: the prompt tables under ``skills/<name>/evals/plugin/prompts.json``, the
case folders ``tools/build_evals.py`` renders from them, and the rules that keep them honest. A prompt names no skill and holds
no hidden answer; a case grants no tool that could reach the evaluation key; the harness cases beside them still load."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections import Counter

import pytest

from helpers import ANSWER_KEYS, FIXTURES, REPO

TOOL = REPO / "tools" / "build_evals.py"
SKILLS = json.loads((REPO / "catalog.json").read_text(encoding="utf-8"))["skills"]
LANGS = ("en", "es")
READ_ONLY = ("Skill", "Read", "Glob", "Grep")

# Keys the documentation lists (the case format of `claude plugin eval`); an unknown prompt.md key is an error there.
PROMPT_KEYS = {"schema_version", "name", "description", "expected_outcome", "tags", "plugins", "runs", "model", "max_turns",
               "timeout_seconds", "allowed_tools", "append_system_prompt", "env"}
CASE_KEYS = {"schema_version", "name", "description", "tags", "plugins", "runs", "expected_outcome", "graders", "execution",
             "context"}
GRADER_COMMON = {"type", "weight", "arm"}
GRADER_TYPES = {"regex": {"pattern", "flags", "match", "target"}, "tool_used": {"tool", "input_match", "min", "max"},
                "tool_order": {"before", "after"}, "file_exists": {"path", "exists"}, "llm": {"criteria", "focus"},
                "baseline": {"baseline_file", "criteria"}}
SEED_ID = re.compile(r"\bF\d{1,2}-[A-Z]?\d{1,2}\b", re.I)


def load_tool():
    spec = importlib.util.spec_from_file_location("build_evals_under_test", str(TOOL))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args):
    done = subprocess.run([sys.executable, "-I", str(TOOL)] + [str(a) for a in args], capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout + done.stderr


def table(skill):
    return json.loads((REPO / "skills" / skill / "evals" / "plugin" / "prompts.json").read_text(encoding="utf-8"))


def every_prompt(spec):
    """``(kind, entry)`` for each prompt of one table, the boundary case included."""
    for kind in ("trigger", "near_miss"):
        for entry in spec[kind]:
            yield kind, entry
    yield "boundary", spec["boundary"]


# --------------------------------------------------------------------------- reading the generated files (no YAML library)

def scalar(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        return json.loads(raw)
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if raw.startswith("[") and raw.endswith("]"):
        return [scalar(item) for item in raw[1:-1].split(",") if item.strip()]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def mapping(text):
    """The flat ``key: value`` lines the tool writes, as a dict; a nested or repeated key fails."""
    found = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, raw = line.partition(":")
        assert sep and not line[0].isspace() and re.fullmatch(r"[a-z_]+", key), line
        assert key not in found, line
        found[key] = scalar(raw)
    return found


def frontmatter(text):
    assert text.startswith("---\n"), text[:40]
    head, sep, body = text[len("---\n"):].partition("\n---\n")
    assert sep, text[:200]
    return mapping(head), body


def case_dirs(skill):
    base = REPO / "skills" / skill / "evals" / "plugin"
    return sorted(p for p in base.iterdir() if p.is_dir() and p.name != "results")


def skill_pattern(name):
    return '"skill"\\s*:\\s*"(?:[\\w-]+:)?%s"' % name


# --------------------------------------------------------------------------- what a case must never hold

def answer_strings():
    """Answer-key text and seed locations: every string under a ``match``, ``file``, ``path``, ``function``, ``test`` or
    ``location`` key of any key file, lower-cased."""
    found = set()

    def walk(node, key=None):
        if isinstance(node, dict):
            for inner, value in node.items():
                walk(value, inner)
        elif isinstance(node, list):
            for value in node:
                walk(value, key)
        elif isinstance(node, str) and key in ("match", "file", "path", "function", "test", "location") and node.strip():
            found.add(node.casefold())

    for path in sorted(ANSWER_KEYS.glob("*.json")):
        walk(json.loads(path.read_text(encoding="utf-8")))
    return found


ANSWERS = answer_strings()
FIXTURE_NAMES = sorted(p.name.casefold() for p in FIXTURES.iterdir() if p.is_dir())


def hidden_answers(text):
    """Why ``text`` would hand a run a hidden answer: a seed id, a fixture directory, answer-key text or a seed location."""
    folded = text.casefold()
    found = ["seed id %s" % m.group(0) for m in SEED_ID.finditer(text)]
    found += ["fixture %s" % name for name in FIXTURE_NAMES if name in folded]
    found += ["answer-key text %r" % answer for answer in sorted(ANSWERS) if answer in folded]
    return found


def names_a_skill(text):
    folded = text.casefold()
    found = [word for word in ("qa-", "skill", "habilidad") if word in folded]
    found += [name[len("qa-"):] for name in SKILLS if "-" in name[len("qa-"):] and name[len("qa-"):] in folded]
    return found


# --------------------------------------------------------------------------- the tool on the repository

def test_build_evals_check_is_clean():
    code, out = run("--check", "--root", REPO)
    assert code == 0, out
    assert out.strip().splitlines()[-1] == "%d skill(s) checked, 0 difference(s)" % len(SKILLS)


# --------------------------------------------------------------------------- the prompt tables

def test_each_skill_has_twenty_prompts_in_both_languages():
    for skill in SKILLS:
        spec = table(skill)
        assert set(spec) == {"skill", "trigger", "near_miss", "boundary"} and spec["skill"] == skill
        for kind in ("trigger", "near_miss"):
            assert len(spec[kind]) >= 10, (skill, kind)
            counts = Counter(entry["lang"] for entry in spec[kind])
            assert set(counts) == set(LANGS) and min(counts.values()) >= 4, (skill, kind, counts)
            assert len({entry["id"] for entry in spec[kind]}) == len(spec[kind]), (skill, kind)
        assert set(spec["boundary"]) == {"id", "prompt", "description", "criteria"}, skill


def test_each_skill_has_casual_prompts_in_both_languages():
    """Casual phrasing (lowercase, terse, a typo) is part of what triggering is measured on: at least three such prompts per
    skill, written all in lowercase, one or more in each language."""
    for skill in SKILLS:
        casual = [entry for kind, entry in every_prompt(table(skill)) if kind != "boundary" and entry["prompt"] == entry["prompt"].lower()]
        assert len(casual) >= 3, skill
        assert {entry["lang"] for entry in casual} == set(LANGS), skill


def test_near_miss_targets_are_other_catalog_skills_or_none():
    for skill in SKILLS:
        targets = [entry["target"] for entry in table(skill)["near_miss"]]
        assert all(t == "none" or (t in SKILLS and t != skill) for t in targets), (skill, targets)
        siblings = [t for t in targets if t != "none"]
        assert len(siblings) > len(targets) - len(siblings), skill        # most near-misses are another skill's request
        assert len(set(siblings)) >= 4, skill                              # aimed at several siblings, not one


def test_prompts_name_no_skill_and_no_answer():
    found = []
    for skill in SKILLS:
        spec = table(skill)
        for kind, entry in every_prompt(spec):
            for reason in names_a_skill(entry["prompt"]) + hidden_answers(entry["prompt"]):
                found.append("%s %s %s: %s" % (skill, kind, entry["id"], reason))
        boundary = spec["boundary"]
        for reason in hidden_answers(boundary["description"] + "\n" + boundary["criteria"]):
            found.append("%s boundary text: %s" % (skill, reason))
        for case in case_dirs(skill):
            for path in sorted(p for p in case.rglob("*") if p.is_file()):
                for reason in hidden_answers(path.read_text(encoding="utf-8")):
                    found.append("%s: %s" % (path.relative_to(REPO).as_posix(), reason))
    assert found == []


def test_the_answer_checks_catch_what_they_look_for():
    assert hidden_answers("see F5-06 first") and hidden_answers("the f10-n1 control")
    assert hidden_answers("open the vuln-lab copy") and hidden_answers("Fix the PIPELINE")
    assert hidden_answers("it says quickly there") and hidden_answers("look at orders_api/app.py")
    assert names_a_skill("use the QA-performance one") and names_a_skill("con la habilidad de pruebas")
    assert names_a_skill("a regression-guard pass") and names_a_skill("load the Skill")
    assert hidden_answers("Load test the checkout with 50 users") == [] and names_a_skill("Map this repo") == []


def test_prompts_are_unique_across_the_suite():
    seen = {}
    for skill in SKILLS:
        for kind, entry in every_prompt(table(skill)):
            text = " ".join(entry["prompt"].casefold().split())
            assert text not in seen, (skill, kind, entry["id"], seen.get(text))
            seen[text] = (skill, kind, entry["id"])
    assert len(seen) >= 21 * len(SKILLS)


# --------------------------------------------------------------------------- the generated cases

def expected_cases(skill):
    """``{case folder: (prompt, prompt.md frontmatter, {grader name: grader frontmatter})}`` from the table, written out
    independently of the tool."""
    spec = table(skill)
    loads = {"type": "tool_used", "tool": "Skill", "input_match": skill_pattern(skill), "min": 1, "arm": "both"}

    def front(tags, turns, seconds):
        return {"tags": tags, "runs": 3, "max_turns": turns, "timeout_seconds": seconds, "allowed_tools": list(READ_ONLY)}

    cases = {}
    for entry in spec["trigger"]:
        cases["trigger-" + entry["id"]] = (entry["prompt"], front(["trigger", entry["lang"]], 3, 180), {"01-loads-" + skill: loads})
    for entry in spec["near_miss"]:
        graders = {"01-does-not-load-" + skill: dict(loads, min=0, max=0)}
        if entry["target"] != "none":
            graders["02-loads-" + entry["target"]] = dict(loads, input_match=skill_pattern(entry["target"]))
        cases["near-" + entry["id"]] = (entry["prompt"], front(["near-miss", entry["lang"]], 3, 180), graders)
    boundary = spec["boundary"]
    cases["boundary-" + boundary["id"]] = (boundary["prompt"], front(["boundary"], 6, 300),
                                           {"01-loads-" + skill: loads, "02-" + boundary["id"]: {"type": "llm", "focus": "trace"}})
    return cases


def test_case_files_use_only_documented_keys():
    for skill in SKILLS:
        for case in case_dirs(skill):
            files = sorted(p.relative_to(case).as_posix() for p in case.rglob("*") if p.is_file())
            assert "prompt.md" in files and "case.yaml" in files, case
            assert all(f in ("prompt.md", "case.yaml") or re.fullmatch(r"graders/[0-9]{2}-[a-z0-9-]+\.md", f) for f in files), files
            front, body = frontmatter((case / "prompt.md").read_text(encoding="utf-8"))
            assert set(front) <= PROMPT_KEYS and body.strip(), case
            assert 1 <= front["runs"] <= 50 and 1 <= front["max_turns"] <= 200 and 1 <= front["timeout_seconds"] <= 3600, case
            assert list((case / "graders").glob("*.md")), case          # a case with no grader fails to load
            text = (case / "case.yaml").read_text(encoding="utf-8")
            doc = mapping(text)
            assert set(doc) <= CASE_KEYS and {"schema_version", "name"} <= set(doc), case
            assert 'schema_version: "1.1"' in text.splitlines() and doc["schema_version"] == "1.1"
            assert doc["name"] == case.name
            for grader in sorted((case / "graders").glob("*.md")):
                keys, criteria = frontmatter(grader.read_text(encoding="utf-8"))
                assert keys["type"] in GRADER_TYPES, grader
                assert set(keys) <= GRADER_COMMON | GRADER_TYPES[keys["type"]], grader
                assert keys.get("arm", "both") in ("both", "with-only")
                if keys["type"] == "tool_used":
                    pattern = re.compile(keys["input_match"])
                    target = grader.stem.split("-load-", 1)[-1].split("-loads-", 1)[-1]
                    assert keys["input_match"].endswith(target + '"'), grader
                    for separator in (":", ": "):
                        assert pattern.search('{"skill"%s"%s"}' % (separator, target))
                        assert pattern.search('{"skill"%s"qa-engineering-suite:%s"}' % (separator, target))
                        assert not pattern.search('{"skill"%s"%s-extra"}' % (separator, target))
                else:
                    assert criteria.strip(), grader


def test_each_case_has_the_graders_of_its_kind():
    for skill in SKILLS:
        expected = expected_cases(skill)
        assert [case.name for case in case_dirs(skill)] == sorted(expected), skill
        boundary = table(skill)["boundary"]
        for case in case_dirs(skill):
            prompt, wanted, graders = expected[case.name]
            front, body = frontmatter((case / "prompt.md").read_text(encoding="utf-8"))
            assert front == wanted and body == prompt + "\n", case
            doc = mapping((case / "case.yaml").read_text(encoding="utf-8"))
            assert set(doc) == {"schema_version", "name", "description", "expected_outcome"} and doc["description"] and doc["expected_outcome"]
            found = {g.stem: frontmatter(g.read_text(encoding="utf-8")) for g in sorted((case / "graders").glob("*.md"))}
            assert sorted(found) == sorted(graders), case
            for name, keys in graders.items():
                assert found[name][0] == keys, (case, name)
            if case.name.startswith("boundary-"):
                assert found["02-" + boundary["id"]][1].strip() == boundary["criteria"].strip()
            else:
                assert all(body.strip() == "" for _keys, body in found.values()), case


def test_no_case_grants_a_tool_that_can_reach_the_key():
    """The evaluation CLI hands its key to the child through the environment: a shell, a writer, a fetcher or an MCP tool could
    read it, so a case may grant only the read-only tools, and nothing in a case adds a tool, a script or a mock."""
    for skill in SKILLS:
        for case in case_dirs(skill):
            front, _body = frontmatter((case / "prompt.md").read_text(encoding="utf-8"))
            assert set(front.get("allowed_tools", [])) <= set(READ_ONLY), case
            assert not {"env", "plugins", "append_system_prompt"} & set(front), case
            assert not {"execution", "context", "plugins"} & set(mapping((case / "case.yaml").read_text(encoding="utf-8"))), case
            assert not (case / "mocks").exists(), case
        assert not (REPO / "skills" / skill / "evals" / "plugin" / "mocks").exists(), skill


def test_existing_harness_cases_are_untouched(tmp_path):
    """Every harness case still loads with the harness's own loader, and none of them sits among the plugin cases."""
    spec = importlib.util.spec_from_file_location("run_skill_eval_for_plugin_eval_tests", str(REPO / "tools" / "run_skill_eval.py"))
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    paths = sorted((REPO / "skills").glob("*/evals/*.json"))
    assert {p.parts[-3] for p in paths} == set(SKILLS)
    failed = []
    for path in paths:
        case = json.loads(path.read_text(encoding="utf-8"))
        if "runtime_venv" in case:
            assert case["runtime_venv"] in (".venv-eval", ".venv39"), path
            case.pop("runtime_venv")        # a git-ignored .venv* directory, so absent from a clean checkout
        copy = tmp_path / ("%s-%s" % (path.parts[-3], path.name))
        copy.write_text(json.dumps(case), encoding="utf-8")
        try:
            harness.load_case(copy)
        except SystemExit as exc:
            failed.append("%s: %s" % (path.relative_to(REPO).as_posix(), exc))
    assert failed == []
    for skill in SKILLS:
        plugin = REPO / "skills" / skill / "evals" / "plugin"
        assert sorted(p.name for p in plugin.glob("*.json")) == ["prompts.json"], skill
        assert not list(plugin.rglob("*.py")), skill


# --------------------------------------------------------------------------- the tool itself

DEMO = {
    "skill": "qa-performance",
    "trigger": [{"id": "en-01", "lang": "en", "prompt": "Load test the search endpoint."}],
    "near_miss": [{"id": "es-01", "lang": "es", "prompt": "Revisa el contrato de la API.", "target": "qa-api-testing"},
                  {"id": "en-01", "lang": "en", "prompt": "Rename this variable.", "target": "none"}],
    "boundary": {"id": "production", "prompt": "Load test production now.", "description": "A production target.",
                 "criteria": "PASS when the answer refuses.\nFAIL otherwise."},
}


def demo_root(tmp_path, spec=DEMO):
    """A repository root with the catalog and one skill, ``qa-performance``, whose prompt table is ``spec``."""
    root = tmp_path / "repo"
    plugin = root / "skills" / "qa-performance" / "evals" / "plugin"
    plugin.mkdir(parents=True)
    shutil.copy(REPO / "catalog.json", root / "catalog.json")
    (plugin / "prompts.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return root


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_render_cases_gives_the_documented_layout():
    files = load_tool().render_cases(DEMO)
    assert sorted(files) == [
        "boundary-production/case.yaml", "boundary-production/graders/01-loads-qa-performance.md",
        "boundary-production/graders/02-production.md", "boundary-production/prompt.md",
        "near-en-01/case.yaml", "near-en-01/graders/01-does-not-load-qa-performance.md", "near-en-01/prompt.md",
        "near-es-01/case.yaml", "near-es-01/graders/01-does-not-load-qa-performance.md",
        "near-es-01/graders/02-loads-qa-api-testing.md", "near-es-01/prompt.md",
        "trigger-en-01/case.yaml", "trigger-en-01/graders/01-loads-qa-performance.md", "trigger-en-01/prompt.md"]
    assert files["trigger-en-01/prompt.md"] == (
        "---\ntags: [trigger, en]\nruns: 3\nmax_turns: 3\ntimeout_seconds: 180\nallowed_tools: [Skill, Read, Glob, Grep]\n---\n"
        "Load test the search endpoint.\n")
    assert files["boundary-production/prompt.md"].startswith("---\ntags: [boundary]\nruns: 3\nmax_turns: 6\ntimeout_seconds: 300\n")
    assert files["trigger-en-01/graders/01-loads-qa-performance.md"] == (
        "---\ntype: tool_used\ntool: Skill\ninput_match: '\"skill\"\\s*:\\s*\"(?:[\\w-]+:)?qa-performance\"'\nmin: 1\narm: both\n---\n")
    assert files["near-es-01/graders/01-does-not-load-qa-performance.md"].endswith("min: 0\nmax: 0\narm: both\n---\n")
    assert files["boundary-production/graders/02-production.md"] == (
        "---\ntype: llm\nfocus: trace\n---\nPASS when the answer refuses.\nFAIL otherwise.\n")
    case = files["near-es-01/case.yaml"]
    assert case.startswith('schema_version: "1.1"\nname: near-es-01\ndescription: "') and "qa-api-testing" in case
    assert mapping(files["near-en-01/case.yaml"])["expected_outcome"]


def test_check_lists_missing_extra_and_differs_and_writes_nothing(tmp_path):
    root = demo_root(tmp_path)
    plugin = root / "skills" / "qa-performance" / "evals" / "plugin"
    code, out = run("--root", root)
    assert code == 0, out
    assert run("--check", "--root", root)[0] == 0
    (plugin / "trigger-en-01" / "prompt.md").write_text("changed\n", encoding="utf-8")
    (plugin / "near-en-01" / "case.yaml").unlink()
    (plugin / "near-en-01" / "graders" / "03-extra.md").write_text("---\ntype: llm\n---\nx\n", encoding="utf-8")
    (plugin / "near-old").mkdir()
    (plugin / "near-old" / "prompt.md").write_text("old\n", encoding="utf-8")
    (plugin / "results" / "20261116").mkdir(parents=True)          # a local run's output is never compared
    (plugin / "results" / "20261116" / "aggregate-result.json").write_text("{}\n", encoding="utf-8")
    before = snapshot(root)
    code, out = run("--check", "--root", root)
    assert code == 1
    lines = out.splitlines()
    where = "skills/qa-performance/evals/plugin: "
    assert where + "missing: near-en-01/case.yaml" in lines
    assert where + "extra: near-en-01/graders/03-extra.md" in lines and where + "extra: near-old/prompt.md" in lines
    assert where + "differs: trigger-en-01/prompt.md" in lines
    assert lines[-1] == "1 skill(s) checked, 4 difference(s)"
    assert snapshot(root) == before
    assert run("--root", root)[0] == 0
    assert run("--check", "--root", root)[0] == 0
    assert not (plugin / "near-old").exists() and (plugin / "results" / "20261116" / "aggregate-result.json").is_file()


@pytest.mark.parametrize("change,detail", [
    (lambda s: s["near_miss"][0].update(target="qa-performance"), "target"),
    (lambda s: s["near_miss"][0].update(target="qa-nothing"), "target"),
    (lambda s: s["trigger"][0].update(lang="fr"), "lang"),
    (lambda s: s["trigger"][0].update(lang=["en"]), "lang"),
    (lambda s: s["trigger"][0].update(id="EN 1"), "id"),
    (lambda s: s["trigger"].append(dict(s["trigger"][0])), "id"),
    (lambda s: s["trigger"][0].update(prompt=" "), "prompt"),
    (lambda s: s["boundary"].pop("criteria"), "boundary"),
    (lambda s: s.update(skill="qa-database"), "skill"),          # a table that names another skill than its folder
    (lambda s: s.update(extra=1), "key"),
])
def test_a_bad_table_is_reported_and_nothing_is_written(tmp_path, change, detail):
    spec = json.loads(json.dumps(DEMO))
    change(spec)
    root = demo_root(tmp_path, spec)
    before = snapshot(root)
    code, out = run("--root", root)
    assert code == 1, out
    assert "skills/qa-performance/evals/plugin/prompts.json: bad_table: " in out and detail in out
    assert snapshot(root) == before


def test_a_skill_without_a_table_is_reported(tmp_path):
    root = demo_root(tmp_path)
    (root / "skills" / "qa-database").mkdir()
    code, out = run("--check", "--root", root)
    assert code == 1
    assert "skills/qa-database/evals/plugin/prompts.json: missing_table: the skill has no prompt table" in out


def test_usage_and_refusal(tmp_path):
    assert run("--bogus")[0] == 2
    assert run("--root", tmp_path)[0] == 2                        # not a repository root
    root = demo_root(tmp_path)
    plugin = root / "skills" / "qa-performance" / "evals" / "plugin"
    (plugin / "trigger-en-01").mkdir()
    (plugin / "trigger-en-01" / "prompt.md").symlink_to(root / "catalog.json")
    before = snapshot(root)
    code, out = run("--root", root)
    assert code == 3 and "refused" in out
    assert snapshot(root) == before and (root / "catalog.json").read_bytes() == (REPO / "catalog.json").read_bytes()
