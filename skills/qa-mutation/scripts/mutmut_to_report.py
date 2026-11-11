"""Convert mutmut 3.x results into a mutation-testing-report-schema JSON file the kernel can ingest.

Usage: python3 mutmut_to_report.py --root DIR --out FILE [--mutants DIR]

``--root`` is the directory mutmut ran in (in this suite, a suite worktree under ``.qa/work/``): it holds the original sources
and, unless ``--mutants`` names another place, the ``mutants/`` directory mutmut wrote. For every ``mutants/<path>.meta`` the
converter reads each mutant's exit code, maps it to mutmut's own status and that status to the schema's, and places the mutant
on the line it changes: the first line where the mutant's function differs from mutmut's unmutated copy, counted from the
function's ``def`` line in the original source (the same line ``mutmut show`` changes). A source file with no mutant is left
out. The report is written only once the kernel's own parser accepts it.

Prints one JSON object: ``ok``, ``out``, ``files``, ``mutants``, ``statuses`` (schema statuses), ``score`` (the schema's
formula over them) and ``undetected`` (each Survived and NoCoverage mutant with its line and change). Exit 0 when written,
2 when refused (``no_mutation_data``, ``unplaced_mutant``, ``report_invalid``). Reads only; writes only ``--out``.

The status table is fixed from mutmut 3.8.1 (``mutmut/stats.py``), checked against the corpus capture of that version.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _mutation  # noqa: E402

# mutmut 3.8.1 ``status_by_exit_code``: exit code of the test run against the mutant -> mutmut's status. Any other code is
# "suspicious", as mutmut's own defaultdict says.
STATUS_BY_EXIT_CODE = {1: "killed", 3: "killed", 0: "survived", 5: "no tests", 2: "check was interrupted by user",
                       None: "not checked", 33: "no tests", 34: "skipped", 35: "suspicious", 36: "timeout",
                       37: "caught by type check", -24: "timeout", 24: "timeout", 152: "timeout", 255: "timeout",
                       -11: "segfault", -9: "segfault"}
# mutmut's status -> the schema's. "no tests" means no test reached the mutated function: the schema's NoCoverage. A mutant
# the type checker rejected never ran against the tests, like one that does not compile. A crash or a suspicious exit says
# nothing about the tests. "skipped" was excluded on purpose; an interrupted or unchecked mutant never got a verdict.
SCHEMA_STATUS = {"killed": "Killed", "survived": "Survived", "no tests": "NoCoverage", "timeout": "Timeout",
                 "caught by type check": "CompileError", "segfault": "RuntimeError", "suspicious": "RuntimeError",
                 "skipped": "Ignored", "check was interrupted by user": "Pending", "not checked": "Pending"}
CLASS_SEPARATOR = "ǁ"  # mutmut's "ǁ" between a class and a method in a mangled name


def mutmut_status(exit_code: Optional[int]) -> str:
    return STATUS_BY_EXIT_CODE.get(exit_code, "suspicious")


def schema_status(exit_code: Optional[int]) -> str:
    return SCHEMA_STATUS[mutmut_status(exit_code)]


def _natural(name: str) -> Tuple[str, int]:
    head, _, number = name.rpartition("__mutmut_")
    return (head, int(number)) if number.isdigit() else (name, -1)


def _functions(tree: ast.AST) -> Dict[str, ast.AST]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _original_def(tree: ast.Module, function: str, cls: Optional[str]) -> Optional[int]:
    """The ``def`` line of ``function`` (in ``cls`` when given) in the original source."""
    if cls is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == cls:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function:
                        return item.lineno
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return node.lineno
    found = [n.lineno for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function]
    return min(found) if found else None


def _names(mutant: str) -> Tuple[str, str, str, Optional[str]]:
    """``(generated mutant function, its unmutated copy, original function, class or None)`` from a mutant name."""
    generated = mutant.rpartition(".")[2]
    mangled = generated.partition("__mutmut_")[0]
    if CLASS_SEPARATOR in mangled:
        cls = mangled[mangled.index(CLASS_SEPARATOR) + 1:mangled.rindex(CLASS_SEPARATOR)]
        function = mangled[mangled.rindex(CLASS_SEPARATOR) + 1:]
    else:
        cls, function = None, mangled[2:] if mangled.startswith("x_") else mangled
    return generated, mangled + "__mutmut_orig", function, cls


def _span(lines: List[str], node: ast.AST) -> List[str]:
    return lines[node.lineno - 1:node.end_lineno]  # type: ignore[attr-defined]


def _place(mutant: str, mutated: List[str], generated_functions: Dict[str, ast.AST], original: ast.Module) -> Dict[str, Any]:
    generated, unmutated, function, cls = _names(mutant)
    copy, changed = generated_functions.get(unmutated), generated_functions.get(generated)
    def_line = _original_def(original, function, cls)
    if copy is None or changed is None or def_line is None:
        raise _mutation.Refusal("unplaced_mutant", "the mutant's function is not in the mutated or the original source",
                                mutant=mutant)
    before = _span(mutated, copy)
    after = _span(mutated, changed)
    if before:
        before = [before[0].replace(unmutated, function, 1)] + before[1:]
    if after:
        after = [after[0].replace(generated, function, 1)] + after[1:]
    index = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)) - 1)
    index = max(index, 0)
    old = before[index] if index < len(before) else ""
    new = after[index] if index < len(after) else ""
    column = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new))) + 1
    line = def_line + index
    return {"line": line, "column": column, "end_column": max(column, len(old.rstrip("\n")) + 1),
            "before": old.strip(), "after": new.strip()}


def convert(root: Path, mutants_dir: Path) -> Dict[str, Dict[str, Any]]:
    if not mutants_dir.is_dir():
        raise _mutation.Refusal("no_mutation_data", "no mutants directory: run mutmut first", mutants=str(mutants_dir))
    files: Dict[str, Dict[str, Any]] = {}
    for meta_path in sorted(mutants_dir.rglob("*.py.meta")):
        relative = PurePosixPath(meta_path.relative_to(mutants_dir).as_posix())
        source_rel = str(relative)[:-len(".meta")]
        try:
            codes = json.loads(meta_path.read_text(encoding="utf-8")).get("exit_code_by_key", {})
        except (OSError, ValueError, AttributeError):
            raise _mutation.Refusal("no_mutation_data", "a mutmut meta file is not readable JSON", file=str(meta_path))
        if not isinstance(codes, dict) or not codes:
            continue
        try:
            original_text = (root / source_rel).read_text(encoding="utf-8")
            mutated_lines = (mutants_dir / source_rel).read_text(encoding="utf-8").splitlines()
            original = ast.parse(original_text)
            generated = _functions(ast.parse("\n".join(mutated_lines)))
        except (OSError, SyntaxError, ValueError) as exc:
            raise _mutation.Refusal("unplaced_mutant", "the original or the mutated source cannot be read: %s" % type(exc).__name__,
                                    file=source_rel)
        mutants = []
        for name in sorted(codes, key=_natural):
            code = codes[name]
            if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
                code = -1  # not an exit code mutmut writes: its default, suspicious
            where = _place(name, mutated_lines, generated, original)
            mutants.append({
                "id": name, "mutatorName": "mutmut", "status": schema_status(code),
                "statusReason": "mutmut: %s (exit code %s)" % (mutmut_status(code), code),
                "location": {"start": _mutation.position(where["line"], where["column"]),
                             "end": _mutation.position(where["line"], where["end_column"])},
                "replacement": where["after"], "description": "%s -> %s" % (where["before"], where["after"]),
            })
        files[source_rel] = {"language": "python", "source": original_text, "mutants": mutants}
    if not files:
        raise _mutation.Refusal("no_mutation_data", "mutmut recorded no mutant under this directory", mutants=str(mutants_dir))
    return files


def run(argv: Optional[List[str]]) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="the directory mutmut ran in")
    parser.add_argument("--mutants", help="mutmut's mutants directory (default: ROOT/mutants)")
    parser.add_argument("--out", required=True, help="the report file to write")
    args = parser.parse_args(argv)
    root = Path(args.root)
    files = convert(root, Path(args.mutants) if args.mutants else root / "mutants")
    return _mutation.finish(files, Path(args.out), "mutmut")


if __name__ == "__main__":
    sys.exit(_mutation.main_wrapper(run, sys.argv[1:]))
