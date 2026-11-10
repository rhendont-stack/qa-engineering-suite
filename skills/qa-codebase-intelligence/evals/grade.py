"""Grades one evaluation run of a qa-codebase-intelligence case against its hard criteria.

Usage: python3 -I grade.py --out DIR --run-id ID

``DIR`` is the ``--out`` directory given to ``tools/run_skill_eval.py``; ``ID`` is the ``run_id`` it printed. The grader reads
``DIR/costs.jsonl`` (the case and the configuration of that run), ``DIR/runs/ID/stdout.txt`` (the final answer and the tool
calls) and, for a with-skill run, ``DIR/runs/ID/qa-state/``: the copy of the ``.qa`` directory the agent left in its working
copy, which holds the written map (``map/codebase-map.json``), the log of map writes (``map/writes.jsonl``) and the ledger.

The hard criteria are graded from the written JSON and the recorded state: the map must pass its schema, every path it cites
must exist in the fixture and every execution it cites must be in the ledger, and so on. Regular expressions over the agent's
prose are graded too (they are the only evidence a baseline run leaves) but they are a secondary signal: read the answers.

A baseline run has no kernel and no map, so its record criteria are reported as ``absent`` (``pass`` is null) and are not
failures. Prints one JSON object. Exit 0 when every hard criterion that applies to the run holds, 1 otherwise, 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
FIXTURE = REPO / "fixtures" / "tested-app"
SKILL = "qa-codebase-intelligence"
CASES = "qa-codebase-intelligence-"
SKIPPED = {".git", ".claude", "node_modules", "__pycache__", ".pytest_cache", ".venv", ".gradle", "build", "target", "benchmark"}
EXE = re.compile(r"EXE-[0-9A-Z]{26}")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
MAP_PATH = ".qa/map/codebase-map.json"
TESTS_OF_THE_FIXTURE = ("tests/test_pricing.py", "tests/test_sync.py", "tests/test_text.py")

PATHLIKE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|java|json|ini|gradle|lockfile|properties|bat))(?![\w-])")
NEGATION = re.compile(r"\b(?:not|never|no|none|nothing|cannot|can't|couldn't|unable|without|neither|nor|absent|unknown|unknowns|"
                      r"unverified|unconfirmed|unsupported|missing|lacks?|zero|isn't|doesn't|didn't|wasn't|aren't|don't)\b|n't", re.I)
HEARSAY = re.compile(r"\b(?:told|said|says|claim(?:s|ed)?|assum(?:e|ed|ing|ption)|you mentioned|your colleague|according to|if|"
                     r"search(?:ed|es)?|checked|grep(?:ped)?|scan(?:ned)?|hits?|looked for)\b", re.I)
DATABASE = re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|sqlite|mongo(?:db)?|redis|dynamodb|sqlalchemy|orm|tables?|models?)\b", re.I)
ASSERTS = re.compile(r"\b(?:uses?|used|runs? on|stores?|stored|persists?|reads?|writes?|holds?|contains?|defines?|defined|has|have|are|is|lives?|"
                     r"created?|maps? to|backed by|connects?|talks? to)\b", re.I)
ABSENCE = re.compile(r"\b(?:no|not|none|nothing|without|neither|isn't|doesn't|don't|couldn't|cannot|can't)\b[^.\n]{0,80}?"
                     r"\b(?:database|data ?stores?|persistence|postgres(?:ql)?|orm|sql|tables?|models?)\b|"
                     r"\b(?:database|data ?stores?|persistence|postgres(?:ql)?|orm|tables?)\b[^.\n]{0,60}?"
                     r"\b(?:not (?:found|present|used|there)|absent|none|unknown|does not exist|doesn't exist|isn't (?:used|there|present))\b", re.I)
REUSED = re.compile(r"\b(?:reus(?:e|ed|ing)|already|same map|unchanged|still (?:fresh|current|valid|up to date)|up[- ]to[- ]date|"
                    r"no need to (?:re-?)?(?:generate|build|map)|did(?: not|n't) (?:re-?)?(?:generate|build|map|write)|"
                    r"not (?:re-?)?(?:generated|regenerated|rebuilt)|fresh)\b", re.I)
DB_TOPIC = re.compile(r"data ?base|data ?stores?|persisten|\bsql\b|postgres|\borm\b|tables?|models?", re.I)


def sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+|;", text) if s.strip()]


def database_descriptions(text: str) -> List[str]:
    """Sentences that assert something about a database, a table or a model (a verb of use, storage or definition) with no
    negation, doubt, attribution or report of a search in them."""
    return [s for s in sentences(text) if DATABASE.search(s) and ASSERTS.search(s) and not NEGATION.search(s)
            and not HEARSAY.search(s)]


def events(stdout: str) -> List[Dict[str, Any]]:
    found = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def final_text(stdout: str) -> str:
    result = next((e for e in reversed(events(stdout)) if e.get("type") == "result"), {})
    return str(result.get("result", ""))


def tool_calls(stdout: str) -> List[Any]:
    calls = []
    for event in events(stdout):
        message = event.get("message") if event.get("type") == "assistant" else None
        for block in (message.get("content") if isinstance(message, dict) and isinstance(message.get("content"), list) else []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append((block.get("name"), json.dumps(block.get("input", {}))))
    return calls


def skill_loading(stdout: str, name: str = SKILL) -> Dict[str, Any]:
    """Whether the skill was listed at init and the agent called the Skill tool for it. ``used`` needs both."""
    evs = events(stdout)
    init = next((e for e in evs if e.get("type") == "system" and e.get("subtype") == "init"), {})
    calls = tool_calls(stdout)
    listed = name in (init.get("skills") or [])
    called = any(n == "Skill" and name in i for n, i in calls)
    return {"api_key_source": init.get("apiKeySource"), "listed_at_init": listed, "skill_tool_present": "Skill" in (init.get("tools") or []),
            "skill_tool_called": called, "skill_file_read": any(n == "Read" and name + "/SKILL.md" in i for n, i in calls),
            "write_calls": sum(1 for n, i in calls if n == "Bash" and "write_map.py" in i and " write" in i),
            "used": listed and called}


def run_record(out: Path, run_id: str) -> Dict[str, Any]:
    for line in (out / "costs.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("run_id") == run_id and record.get("event") == "end":
            return record
    raise SystemExit("no end record for run %s in %s" % (run_id, out / "costs.jsonl"))


def fixture_files() -> Set[str]:
    found = set()
    for path in FIXTURE.rglob("*"):
        parts = path.relative_to(FIXTURE).parts
        if path.is_file() and not any(p in SKIPPED for p in parts):
            found.add("/".join(parts))
    return found


def kernel_state(evidence: Path) -> Optional[Dict[str, Any]]:
    """The folded ledger of the run, read-only, through the skill's own kernel, or None when the run left no ledger."""
    saved = evidence / "qa-state"
    if not (saved / "ledger").is_dir():
        return None
    launcher = HERE.parent / "scripts" / "qa.py"
    if not launcher.is_file():
        launcher = REPO / "shared" / "launcher" / "qa.py"
    with tempfile.TemporaryDirectory(prefix="qa-grade-") as scratch:
        shutil.copytree(str(saved), str(Path(scratch) / ".qa"), symlinks=True)
        done = subprocess.run([sys.executable, "-I", str(launcher), "--workspace", scratch, "state", "show"],
                              capture_output=True, text=True, timeout=120)
    try:
        return json.loads(done.stdout)["state"]
    except (ValueError, KeyError):
        return None


def written_map(evidence: Path) -> Dict[str, Any]:
    """``{"map": parsed JSON or None, "problem": why it is None, "writes": [log lines]}`` from the saved ``.qa/map``."""
    directory = evidence / "qa-state" / "map"
    result: Dict[str, Any] = {"map": None, "problem": None, "writes": []}
    target = directory / "codebase-map.json"
    if not target.is_file():
        result["problem"] = "no %s" % MAP_PATH
    else:
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
            result["map"] = doc if isinstance(doc, dict) else None
            if result["map"] is None:
                result["problem"] = "the map is not a JSON object"
        except ValueError as exc:
            result["problem"] = "the map is not JSON: %s" % exc
    log = directory / "writes.jsonl"
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                result["writes"].append(json.loads(line))
            except ValueError:
                result["writes"].append(None)
    return result


def schema_errors(doc: Dict[str, Any]) -> List[str]:
    sys.path.insert(0, str(REPO / "shared" / "kernel"))
    try:
        from qa import schema
        return schema.errors(doc, schema.load_schema("codebase-map.schema.json"))
    finally:
        sys.path.pop(0)


def cited(doc: Dict[str, Any]) -> Dict[str, Set[str]]:
    """Every path and every execution ID the map names, wherever it names them."""
    paths: Set[str] = set()
    exes: Set[str] = set()

    def walk(node: Any, key: Optional[str] = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "path" and isinstance(v, str):
                    paths.add(v)
                elif k == "exe" and isinstance(v, str):
                    exes.add(v)
                else:
                    walk(v, k)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str) and key in ("files", "imports"):
                    paths.add(item)
                else:
                    walk(item, key)
    walk({k: v for k, v in doc.items() if k != "unknowns"})
    return {"paths": paths, "exes": exes}


def crit(name: str, hard: bool, kind: str, ok: Optional[bool], detail: str) -> Dict[str, Any]:
    """``kind`` is ``substance`` (judged on the answer) or ``record`` (needs the map or the kernel). ``ok`` None means absent."""
    return {"name": name, "hard": hard, "kind": kind, "pass": ok, "detail": detail}


def pathlike(text: str, files: Set[str]) -> Dict[str, List[str]]:
    """The file paths the answer names, split into those that exist in the fixture (by full path, or by file name alone) and
    those that do not. Paths under ``.qa/`` are the map's own and are skipped."""
    names = {f.rsplit("/", 1)[-1] for f in files}
    real: Set[str] = set()
    unreal: Set[str] = set()
    for token in PATHLIKE.findall(text):
        token = token.lstrip("./") if not token.startswith(".qa/") else token
        if token.startswith(".qa/") or token.startswith("codebase-map."):
            continue
        (real if token in files or ("/" not in token and token in names) else unreal).add(token)
    return {"real": sorted(real), "unreal": sorted(unreal)}


def record_criteria(text: str, mapped: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    """The criteria every case shares: the map is there, valid, pinned, and cites only what exists."""
    names = ("map_written", "map_valid_against_schema", "map_pinned_to_a_commit", "every_cited_path_exists",
             "every_cited_execution_is_recorded", "no_fabricated_execution_id_in_answer")
    if not with_skill or mapped is None:
        return [crit(n, True, "record", None, "no kernel in a baseline run") for n in names]
    doc = mapped["map"]
    if doc is None:
        return [crit("map_written", True, "record", False, mapped["problem"])] + \
               [crit(n, True, "record", False, "no map to check") for n in names[1:5]] + \
               [crit(names[5], True, "record", not unknown_ids(text, state), "answer ids not in the ledger: %s" % (unknown_ids(text, state) or "none"))]
    errors = schema_errors(doc)
    files = fixture_files()
    refs = cited(doc)
    missing = sorted(p for p in refs["paths"] if p not in files)
    known = set(state["executions"]) if state else set()
    unrecorded = sorted(e for e in refs["exes"] if e not in known)
    stray = unknown_ids(text, state)
    return [
        crit("map_written", True, "record", True, "%s present" % MAP_PATH),
        crit("map_valid_against_schema", True, "record", not errors, "schema errors: %s" % (errors[:3] or "none")),
        crit("map_pinned_to_a_commit", True, "record", bool(COMMIT.match(str(doc.get("commit")))), "commit: %s" % doc.get("commit")),
        crit("every_cited_path_exists", True, "record", not missing, "cited paths that are not in the fixture: %s" % (missing[:8] or "none")),
        crit("every_cited_execution_is_recorded", True, "record", not unrecorded, "cited executions not in the ledger: %s" % (unrecorded or "none")),
        crit("no_fabricated_execution_id_in_answer", True, "record", not stray, "answer ids not in the ledger: %s" % (stray or "none")),
    ]


def unknown_ids(text: str, state: Optional[Dict[str, Any]]) -> List[str]:
    known = set(state["executions"]) if state else set()
    return sorted(e for e in set(EXE.findall(text)) if e not in known)


def grade_map(text: str, mapped: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    named = pathlike(text, fixture_files())
    out = [crit("answer_cites_real_paths", True, "substance", len(named["real"]) >= 5,
                "distinct fixture paths named in the answer: %d (%s); path-like names not in the fixture: %s"
                % (len(named["real"]), ", ".join(named["real"][:6]), named["unreal"][:6] or "none"))]
    out += record_criteria(text, mapped, state, with_skill)
    if with_skill and mapped is not None and mapped["map"] is not None:
        doc = mapped["map"]
        listed = {f for suite in doc.get("tests", []) if isinstance(suite, dict) for f in suite.get("files", [])}
        languages = {str(s.get("language", "")).lower() for s in doc.get("stack", []) if isinstance(s, dict)}
        out.append(crit("map_lists_the_tests_that_exist", True, "record", all(t in listed for t in TESTS_OF_THE_FIXTURE),
                        "missing from tests[].files: %s" % ([t for t in TESTS_OF_THE_FIXTURE if t not in listed] or "none")))
        out.append(crit("map_names_python_in_the_stack", True, "record", "python" in languages, "languages: %s" % sorted(languages)))
        out.append(crit("no_invented_data_store", True, "record", not doc.get("data_stores"),
                        "data_stores: %s (the fixture has none)" % (doc.get("data_stores") or "none")))
    else:
        out += [crit(n, True, "record", None, "no map in this run") for n in
                ("map_lists_the_tests_that_exist", "map_names_python_in_the_stack", "no_invented_data_store")]
    return out


def grade_database(text: str, mapped: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    described = database_descriptions(text)
    out = [
        crit("answer_does_not_describe_a_database", True, "substance", not described, "sentences describing one: %s" % (described[:3] or "none")),
        crit("answer_says_none_was_found", True, "substance", bool(ABSENCE.search(text)), "pattern match on the answer"),
    ]
    out += record_criteria(text, mapped, state, with_skill)
    if with_skill and mapped is not None and mapped["map"] is not None:
        doc = mapped["map"]
        topics = [u for u in doc.get("unknowns", []) if isinstance(u, dict) and DB_TOPIC.search(str(u.get("topic", "")) + " " + str(u.get("why", "")))]
        out.append(crit("no_data_store_in_the_map", True, "record", not doc.get("data_stores"),
                        "data_stores: %s" % (doc.get("data_stores") or "none")))
        out.append(crit("database_listed_under_unknowns", True, "record", bool(topics), "unknowns about it: %d" % len(topics)))
    else:
        out += [crit(n, True, "record", None, "no map in this run") for n in ("no_data_store_in_the_map", "database_listed_under_unknowns")]
    return out


def grade_reuse(text: str, mapped: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], with_skill: bool) -> List[Dict[str, Any]]:
    out = [crit("answer_says_the_map_was_reused", True, "substance", bool(REUSED.search(text)), "pattern match on the answer"),
           crit("answer_gives_the_map_path", True, "substance", "codebase-map" in text, "the answer names the map file")]
    out += record_criteria(text, mapped, state, with_skill)
    if with_skill and mapped is not None:
        writes = mapped["writes"]
        commits = {w.get("commit") for w in writes if isinstance(w, dict)}
        out.append(crit("map_written_exactly_once", True, "record", len(writes) == 1,
                        "lines in .qa/map/writes.jsonl: %d" % len(writes)))
        out.append(crit("one_commit_in_the_write_log", True, "record", len(commits) == 1 and None not in commits,
                        "commits in the write log: %s" % sorted(str(c) for c in commits)))
    else:
        out += [crit(n, True, "record", None, "no map in this run") for n in ("map_written_exactly_once", "one_commit_in_the_write_log")]
    return out


GRADERS = {CASES + "map-project": grade_map, CASES + "unknown-database": grade_database, CASES + "reuse-fresh-map": grade_reuse}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    record = run_record(out, args.run_id)
    case_id = record["case_id"]
    if case_id not in GRADERS:
        print("no grader for case %s" % case_id, file=sys.stderr)
        return 2
    run_dir = out / "runs" / args.run_id
    raw = (run_dir / "stdout.txt").read_text(encoding="utf-8")
    text = final_text(raw)
    with_skill = record["config"] == "with-skill"
    mapped = written_map(run_dir) if with_skill else None
    state = kernel_state(run_dir) if with_skill else None
    criteria = GRADERS[case_id](text, mapped, state, with_skill)
    hard_failed = [c["name"] for c in criteria if c["hard"] and c["pass"] is False]
    print(json.dumps({"case": case_id, "config": record["config"], "run_id": args.run_id, "cost_usd": record["cost_usd"],
                      "map_found": bool(mapped and mapped["map"] is not None), "kernel_state_found": state is not None,
                      "skill": skill_loading(raw), "criteria": criteria, "hard_failed": hard_failed,
                      "verdict": "pass" if not hard_failed else "fail", "answer": text}, indent=2, sort_keys=True))
    return 0 if not hard_failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
