"""Builds every skill's ``claude plugin eval`` cases from its prompt table, or checks that the built cases are current.

Usage: python3 -I tools/build_evals.py [--check] [--root <repository>]

Each ``skills/<name>/evals/plugin/prompts.json`` holds ``{"skill", "trigger", "near_miss", "boundary"}``. From it this tool
writes one case folder per prompt beside the table: ``trigger-<id>/`` (the run must load the skill),
``near-<id>/`` (it must not; when the entry names a ``target`` skill, that one must load) and ``boundary-<id>/`` (the skill
loads and keeps one of its rules, judged from the trace). Every case holds ``prompt.md`` (frontmatter, then the prompt),
``case.yaml`` and ``graders/*.md``, and grants only the read-only tools Skill, Read, Glob and Grep. Run one skill's cases with
``claude plugin eval <suite root> --eval-dir skills/<name>/evals/plugin``.

Writing replaces the case folders with exactly what the table gives. ``--check`` writes nothing: it prints one line per
difference (``missing:``, ``extra:``, ``differs:``) and exits 1 if there is any. ``results/``, where a local run of the
evaluation CLI puts its output, is never compared or removed. A table that is not well formed is reported as
``<path>: bad_table: <detail>`` and nothing is written for that skill; a skill with no table is ``missing_table``.

Exit 0 ok, 1 problems, 2 usage or not a repository root, 3 refused (a link where a case file goes: nothing is written).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

PLUGIN = ("evals", "plugin")
TABLE = "prompts.json"
RESULTS = "results"
LANGUAGES = {"en": "English", "es": "Spanish"}
ALLOWED_TOOLS = ("Skill", "Read", "Glob", "Grep")  # read-only: the evaluation CLI's key is in the run's environment
RUNS = 3
TRIGGER_LIMITS = (3, 180)     # max_turns, timeout_seconds for trigger and near-miss cases
BOUNDARY_LIMITS = (6, 300)
SCHEMA_VERSION = "1.1"
ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
KEYS = {"skill", "trigger", "near_miss", "boundary"}
ENTRY_KEYS = {"trigger": {"id", "lang", "prompt"}, "near_miss": {"id", "lang", "prompt", "target"},
              "boundary": {"id", "prompt", "description", "criteria"}}


def _quoted(text: str) -> str:
    """A YAML double-quoted scalar: a JSON string is one."""
    return json.dumps(text, ensure_ascii=False)


def _skill_match(skill: str) -> str:
    """The documented Skill-tool pattern: the bare name or ``<plugin>:<name>``, ending at the closing quote."""
    return "'\"skill\"\\s*:\\s*\"(?:[\\w-]+:)?%s\"'" % skill


def _prompt_md(tags: Sequence[str], limits: Sequence[int], prompt: str) -> str:
    return ("---\ntags: [%s]\nruns: %d\nmax_turns: %d\ntimeout_seconds: %d\nallowed_tools: [%s]\n---\n%s\n"
            % (", ".join(tags), RUNS, limits[0], limits[1], ", ".join(ALLOWED_TOOLS), prompt))


def _case_yaml(name: str, description: str, outcome: str) -> str:
    return 'schema_version: "%s"\nname: %s\ndescription: %s\nexpected_outcome: %s\n' % (
        SCHEMA_VERSION, name, _quoted(description), _quoted(outcome))


def _skill_grader(skill: str, loads: bool) -> str:
    counts = "min: 1\n" if loads else "min: 0\nmax: 0\n"
    return "---\ntype: tool_used\ntool: Skill\ninput_match: %s\n%sarm: both\n---\n" % (_skill_match(skill), counts)


def render_cases(spec: Mapping[str, Any]) -> Dict[str, str]:
    """``{path relative to evals/plugin/: file text}`` for every case of a well-formed table (see ``table_problems``)."""
    skill = spec["skill"]
    files: Dict[str, str] = {}
    for entry in spec["trigger"]:
        name = "trigger-" + entry["id"]
        language = LANGUAGES[entry["lang"]]
        files[name + "/prompt.md"] = _prompt_md(["trigger", entry["lang"]], TRIGGER_LIMITS, entry["prompt"])
        files[name + "/case.yaml"] = _case_yaml(
            name, "A request in %s that %s owns: the run must load it." % (language, skill),
            "Claude loads %s through the Skill tool." % skill)
        files[name + "/graders/01-loads-%s.md" % skill] = _skill_grader(skill, True)
    for entry in spec["near_miss"]:
        name = "near-" + entry["id"]
        language, target = LANGUAGES[entry["lang"]], entry["target"]
        files[name + "/prompt.md"] = _prompt_md(["near-miss", entry["lang"]], TRIGGER_LIMITS, entry["prompt"])
        files[name + "/graders/01-does-not-load-%s.md" % skill] = _skill_grader(skill, False)
        if target == "none":
            description = "A request in %s near %s's area that no skill of the suite owns." % (language, skill)
            outcome = "Claude does not load %s." % skill
        else:
            description = "A request in %s that %s owns, not %s." % (language, target, skill)
            outcome = "Claude does not load %s; it loads %s." % (skill, target)
            files[name + "/graders/02-loads-%s.md" % target] = _skill_grader(target, True)
        files[name + "/case.yaml"] = _case_yaml(name, description, outcome)
    boundary = spec["boundary"]
    name = "boundary-" + boundary["id"]
    files[name + "/prompt.md"] = _prompt_md(["boundary"], BOUNDARY_LIMITS, boundary["prompt"])
    files[name + "/case.yaml"] = _case_yaml(
        name, boundary["description"],
        "Claude loads %s, and its answer meets the criteria in graders/02-%s.md." % (skill, boundary["id"]))
    files[name + "/graders/01-loads-%s.md" % skill] = _skill_grader(skill, True)
    files[name + "/graders/02-%s.md" % boundary["id"]] = "---\ntype: llm\nfocus: trace\n---\n%s\n" % boundary["criteria"].strip()
    return files


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def table_problems(spec: Any, folder: str, skills: Sequence[str]) -> List[str]:
    """Why ``spec`` (the parsed table of ``skills/<folder>/``) cannot be rendered; empty when it can."""
    if not isinstance(spec, dict):
        return ["the table is not a JSON object"]
    found = ["key %r is unexpected" % key for key in sorted(set(spec) - KEYS)]
    found += ["key %r is missing" % key for key in sorted(KEYS - set(spec))]
    if spec.get("skill") != folder:
        found.append("skill must be %r, the folder's name" % folder)
    for kind in ("trigger", "near_miss"):
        entries = spec.get(kind)
        if not isinstance(entries, list) or not entries:
            found.append("%s must be a non-empty list" % kind)
            continue
        seen = set()
        for number, entry in enumerate(entries):
            where = "%s[%d]" % (kind, number)
            if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS[kind]:
                found.append("%s must hold exactly the keys %s" % (where, ", ".join(sorted(ENTRY_KEYS[kind]))))
                continue
            found += _id_problems(where, entry["id"], seen)
            if not isinstance(entry["lang"], str) or entry["lang"] not in LANGUAGES:
                found.append("%s lang must be one of %s" % (where, ", ".join(sorted(LANGUAGES))))
            if not _text(entry["prompt"]):
                found.append("%s prompt must be non-empty text" % where)
            if kind == "near_miss" and not (entry["target"] == "none" or (entry["target"] in skills and entry["target"] != folder)):
                found.append("%s target must be another skill of the catalog or \"none\"" % where)
    boundary = spec.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != ENTRY_KEYS["boundary"]:
        found.append("boundary must hold exactly the keys %s" % ", ".join(sorted(ENTRY_KEYS["boundary"])))
    else:
        found += _id_problems("boundary", boundary["id"], set())
        found += ["boundary %s must be non-empty text" % key for key in ("prompt", "description", "criteria")
                  if not _text(boundary[key])]
    return found


def _id_problems(where: str, ident: Any, seen: set) -> List[str]:
    if not isinstance(ident, str) or not ID.match(ident):
        return ["%s id must be lowercase letters and digits joined by single hyphens" % where]
    if ident in seen:
        return ["%s id %r is used twice" % (where, ident)]
    seen.add(ident)
    return []


def built_files(plugin: Path) -> Dict[str, Optional[str]]:
    """``{path relative to plugin/: text}`` of what the case folders hold now; a link or a non-file maps to None."""
    found: Dict[str, Optional[str]] = {}
    for here, directories, names in os.walk(str(plugin), followlinks=False):
        relative_dir = os.path.relpath(here, str(plugin))
        if relative_dir == ".":
            directories[:] = sorted(d for d in directories if d != RESULTS)
            names = [n for n in names if n not in (TABLE, RESULTS)]  # write() leaves both alone
        for directory in list(directories):
            if os.path.islink(os.path.join(here, directory)):
                found[os.path.normpath(os.path.join(relative_dir, directory)).replace(os.sep, "/")] = None
                directories.remove(directory)
        for name in sorted(names):
            if name == ".DS_Store":
                continue
            path = os.path.join(here, name)
            relative = os.path.normpath(os.path.join(relative_dir, name)).replace(os.sep, "/")
            if not stat.S_ISREG(os.lstat(path).st_mode):
                found[relative] = None
                continue
            with open(path, "rb") as handle:
                found[relative] = handle.read().decode("utf-8", errors="replace")
    return found


def differences(expected: Mapping[str, str], actual: Mapping[str, Optional[str]]) -> List[str]:
    found = ["missing: " + name for name in sorted(set(expected) - set(actual))]
    found += ["extra: " + name for name in sorted(set(actual) - set(expected))]
    found += ["differs: " + name for name in sorted(set(expected) & set(actual)) if expected[name] != actual[name]]
    return found


def unsafe(plugin: Path, actual: Mapping[str, Optional[str]]) -> List[str]:
    """Links or non-files where the tool would write or delete; nothing behind them is followed."""
    found = ["unsafe: %s is a link or not a regular file" % name for name, text in sorted(actual.items()) if text is None]
    if os.path.islink(str(plugin)) or os.path.islink(str(plugin.parent)):
        found.insert(0, "unsafe: %s is a link" % "/".join(PLUGIN))
    return found


def write(plugin: Path, expected: Mapping[str, str]) -> None:
    """Replaces every case folder under ``plugin`` with ``expected``; the table and ``results/`` stay."""
    for entry in sorted(plugin.iterdir()):  # unsafe() has already refused any link in here
        if entry.name in (TABLE, RESULTS, ".DS_Store"):
            continue
        if entry.is_dir():
            shutil.rmtree(str(entry))
        else:
            entry.unlink()
    for name, text in sorted(expected.items()):
        target = plugin / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


def catalog_skills(root: Path) -> Optional[List[str]]:
    try:
        skills = json.loads((root / "catalog.json").read_text(encoding="utf-8"))["skills"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return [s for s in skills if isinstance(s, str)] if isinstance(skills, list) else None


def main(argv: List[str]) -> int:
    sys.stdout.reconfigure(errors="backslashreplace")
    check = "--check" in argv
    rest = [a for a in argv if a != "--check"]
    root = Path(".")
    if rest[:1] == ["--root"] and len(rest) == 2:
        root = Path(rest[1])
    elif rest:
        print("usage: build_evals.py [--check] [--root <repository>]")
        return 2
    skills = catalog_skills(root)
    if skills is None or not (root / "skills").is_dir():
        print("build_evals.py: not a repository root (no catalog.json skill list or skills/): %s" % root)
        return 2
    folders = sorted(p for p in (root / "skills").iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    problems: List[str] = []
    plans = []
    for folder in folders:
        plugin = folder.joinpath(*PLUGIN)
        where = plugin.relative_to(root).as_posix()
        try:
            spec = json.loads((plugin / TABLE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            problems.append("%s/%s: missing_table: the skill has no prompt table" % (where, TABLE))
            continue
        except (OSError, ValueError) as exc:
            problems.append("%s/%s: bad_table: not readable JSON (%s)" % (where, TABLE, type(exc).__name__))
            continue
        bad = table_problems(spec, folder.name, skills)
        if bad:
            problems += ["%s/%s: bad_table: %s" % (where, TABLE, detail) for detail in bad]
            continue
        actual = built_files(plugin)
        plans.append((where, plugin, render_cases(spec), actual, unsafe(plugin, actual)))
    if check:
        for where, _plugin, expected, actual, refused in plans:
            problems += ["%s: %s" % (where, line) for line in refused or differences(expected, actual)]
        for line in problems:
            print(line)
        print("%d skill(s) checked, %d difference(s)" % (len(folders), len(problems)))
        return 1 if problems else 0
    refused = ["%s: %s" % (where, line) for where, _plugin, _expected, _actual, lines in plans for line in lines]
    if refused:  # every refusal comes before the first write or delete
        for line in refused:
            print(line)
        print("refused: nothing was written")
        return 3
    cases = 0
    for where, plugin, expected, _actual, _refused in plans:
        write(plugin, expected)
        count = len({name.split("/", 1)[0] for name in expected})
        cases += count
        print("built %s (%d cases)" % (where, count))
    for line in problems:
        print(line)
    print("%d skill(s) built, %d case(s), %d problem(s)" % (len(plans), cases, len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
