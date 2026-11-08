from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

import pytest

from helpers import REPO

TOOLS = REPO / "tools"
GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not GIT, reason="tool missing: git")


# --------------------------------------------------------------------------- check_docs

def check_docs(tmp_path, p1, p2, brief):
    files = [tmp_path / n for n in ("p1.md", "p2.md", "brief.md")]
    for f, text in zip(files, (p1, p2, brief)): f.write_text(text)
    return subprocess.run([sys.executable, "-I", str(TOOLS / "check_docs.py"), *map(str, files)], capture_output=True, text=True)
P2 = "## 1. Intro\n\n### 22.1 Coverage\n\n| Brief | Requirement | Addressed in |\n|---|---|---|\n| BR-1.01 | x | §1 |\n"
def test_check_docs_skips_section_signs_in_external_link_text(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n\nSee [Card §3.8](https://example.com/card.pdf).\n", P2, "- **BR-1.01** Text.\n")
    assert r.returncode == 0, r.stdout
def test_check_docs_reports_uncovered_brief_id(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n", P2, "IDs look like `BR-9.99`.\n- **BR-1.01** Text.\n- **BR-1.02** More.\n")
    assert r.returncode == 1 and "BR-1.02" in r.stdout and "BR-9.99" not in r.stdout
def test_check_docs_expands_id_ranges(tmp_path):
    p2 = P2.replace("| BR-1.01 |", "| BR-1.01–BR-1.03 |")
    assert check_docs(tmp_path, "## 1. A\n", p2, "- **BR-1.01** A.\n- **BR-1.02** B.\n- **BR-1.03** C.\n").returncode == 0
    assert check_docs(tmp_path, "## 1. A\n", P2.replace("| BR-1.01 |", "| BR-1.03–BR-1.01 |"), "- **BR-1.01** A.\n- **BR-1.03** C.\n").returncode == 1


# T19-R3, and the brief's other failure forms
def test_check_docs_first_column_can_hold_a_comma_list_and_repeats(tmp_path):
    p2 = P2.replace("| BR-1.01 |", "| BR-1.01, BR-1.02–BR-1.03, BR-1.01 |")
    r = check_docs(tmp_path, "## 1. A\n", p2, "- **BR-1.01** A.\n- **BR-1.02** B.\n- **BR-1.03** C.\n")
    assert r.returncode == 0, r.stdout
def test_check_docs_range_with_an_unknown_end_is_a_problem(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n", P2.replace("| BR-1.01 |", "| BR-1.01–BR-1.09 |"), "- **BR-1.01** A.\n")
    assert r.returncode == 1 and "BR-1.09" in r.stdout
    r = check_docs(tmp_path, "## 1. A\n", P2.replace("| BR-1.01 |", "| BR-1.00–BR-1.01 |"), "- **BR-1.01** A.\n")
    assert r.returncode == 1 and "BR-1.00" in r.stdout
def test_check_docs_cited_id_the_brief_does_not_define_is_a_problem(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n", P2.replace("| BR-1.01 |", "| BR-1.01, BR-7.77 |"), "- **BR-1.01** A.\n")
    assert r.returncode == 1 and "BR-7.77" in r.stdout
def test_check_docs_a_part_of_an_id_counts_as_the_id(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n", P2.replace("| BR-1.01 |", "| BR-1.01(c) |"), "- **BR-1.01** A.\n")
    assert r.returncode == 0, r.stdout

DEVIATIONS = ("\n### 22.2 Deviations\n\n| # | The brief | This design | Why |\n|---|---|---|---|\n| 1 | a | b | c |\n| 2 | a | b | c |\n"
              "\n### 22.3 Deviations introduced by the milestone 1 plan\n\n| # | Deviation | Reason |\n|---|---|---|\n{rows}")
def deviations(*numbers): return P2 + DEVIATIONS.format(rows="".join("| %d | d | r |\n" % n for n in numbers))
def test_check_docs_deviation_numbers_continue_across_22_2_and_22_3(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n", deviations(3, 4) + "\nSee deviation #4 and #1.\n", "- **BR-1.01** A.\n")
    assert r.returncode == 0, r.stdout
def test_check_docs_gap_in_deviation_numbers_is_a_problem(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n", deviations(4), "- **BR-1.01** A.\n")
    assert r.returncode == 1 and "deviation" in r.stdout
def test_check_docs_cited_deviation_must_exist(tmp_path):
    r = check_docs(tmp_path, "## 1. A\n", deviations(3) + "\nSee deviation #9.\n", "- **BR-1.01** A.\n")
    assert r.returncode == 1 and "#9" in r.stdout
def test_check_docs_usage_error_without_three_files():
    r = subprocess.run([sys.executable, "-I", str(TOOLS / "check_docs.py")], capture_output=True, text=True)
    assert r.returncode == 2 and "usage" in (r.stdout + r.stderr).lower()
def test_check_docs_passes_on_the_proposal():
    docs = REPO / "docs"
    r = subprocess.run([sys.executable, "-I", str(TOOLS / "check_docs.py"), str(docs / "proposal" / "phase-1-research.md"),
                        str(docs / "proposal" / "phase-2-architecture.md"), str(docs / "requirements" / "brief.md")],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.endswith("0 problem(s)\n"), r.stdout


# --------------------------------------------------------------------------- check_repo

def check_repo(*args, cwd=None, env=None, timeout=60):
    return subprocess.run([sys.executable, "-I", str(TOOLS / "check_repo.py"), *map(str, args)], capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=timeout)
def put(root, name, content="x\n"):
    p = root / name; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8")); return p
def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)
def git_repo(root):
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    return root
def modes_bind():
    return os.name == "posix" and os.geteuid() != 0
def problems(r):
    return [line for line in r.stdout.splitlines()[:-1]]

@pytest.mark.parametrize("name,content,kind", [("a.md", "", "empty"), ("b.md", "TODO\n", "placeholder"),
                                               ("c.md", "# Title\n", "placeholder"), ("d.json", "{", "invalid_json"),
                                               ("shared/policies/command-guard.json", '{"version": 1, "rules": "none"}', "policy_invalid"),
                                               ("shared/schemas/events/x.y.schema.json", '{"type": "object"}', "schema_not_closed")])
def test_check_repo_flags(tmp_path, name, content, kind):
    p = tmp_path / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
    r = subprocess.run([sys.executable, "-I", str(REPO / "tools" / "check_repo.py"), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 1 and kind in r.stdout
@needs_git
def test_check_repo_passes_on_this_repository():
    assert subprocess.run([sys.executable, "-I", str(REPO / "tools" / "check_repo.py"), str(REPO)]).returncode == 0


# T19-R7 content rules
def test_zero_bytes_is_empty_not_placeholder(tmp_path):
    put(tmp_path, "a.md", b"")
    assert problems(check_repo(tmp_path)) == ["a.md: empty: the file has no bytes"]
@pytest.mark.parametrize("content", [b" \n\t\n", b"todo", b"Tbd\n", b"PLACEHOLDER", b"...\n", b"\n# Title\n\n", b"##Heading no space"])
def test_placeholder_forms(tmp_path, content):
    put(tmp_path, "a.md", content)
    r = check_repo(tmp_path)
    assert r.returncode == 1 and problems(r) == ["a.md: placeholder: the whole content is a placeholder"]
@pytest.mark.parametrize("content", [b"# a\n# b\n", b"# a\ntext\n", b"to do\n", b"TODO: later\n", b"\xff\xfe\n", b"x"])
def test_real_content_is_not_a_placeholder(tmp_path, content):
    put(tmp_path, "a.md", content)
    r = check_repo(tmp_path)
    assert r.returncode == 0 and r.stdout == "0 problem(s)\n"
def test_json_that_is_not_utf8_is_invalid_json(tmp_path):
    put(tmp_path, "a.json", b'{"k": "\xff"}')
    assert problems(check_repo(tmp_path))[0].startswith("a.json: invalid_json")
def test_unparseable_policy_and_event_schema_report_invalid_json_only(tmp_path):
    put(tmp_path, "shared/policies/redaction.json", "{")
    put(tmp_path, "shared/schemas/events/x.y.schema.json", "[")
    r = check_repo(tmp_path)
    assert [p.split(": ")[:2] for p in problems(r)] == [["shared/policies/redaction.json", "invalid_json"],
                                                         ["shared/schemas/events/x.y.schema.json", "invalid_json"]]


# T19-R8 policies
def test_every_real_policy_validates_under_its_mapped_schema(tmp_path):
    shutil.copytree(REPO / "shared" / "policies", tmp_path / "shared" / "policies")
    r = check_repo(tmp_path)
    assert r.returncode == 0 and r.stdout == "0 problem(s)\n", r.stdout
@pytest.mark.parametrize("name", ["redaction.json", "command-guard.json", "severity-rubric.json", "priority-matrix.json",
                                  "gate.audit.json", "gate.change.json", "gate.fix.json", "gate.release.json", "gate.custom.json"])
def test_each_mapped_policy_is_checked_against_its_schema(tmp_path, name):
    put(tmp_path, "shared/policies/" + name, '{"nonsense": true}')
    r = check_repo(tmp_path)
    assert r.returncode == 1 and problems(r)[0].startswith("shared/policies/%s: policy_invalid" % name)
def test_a_policy_with_no_mapped_schema_is_policy_invalid(tmp_path):
    put(tmp_path, "shared/policies/new-policy.json", "{}")
    assert problems(check_repo(tmp_path))[0].startswith("shared/policies/new-policy.json: policy_invalid")
def test_policies_are_not_searched_recursively(tmp_path):
    put(tmp_path, "shared/policies/sub/x.json", "{}")
    assert check_repo(tmp_path).returncode == 0


# T19-R9 closed event schemas
CLOSED = '{"additionalProperties": false}'
def test_event_schema_closed_through_a_oneof_passes(tmp_path):
    put(tmp_path, "shared/schemas/events/a.b.schema.json", '{"oneOf": [%s, %s]}' % (CLOSED, CLOSED))
    r = check_repo(tmp_path)
    assert r.returncode == 0, r.stdout
def test_event_schema_with_one_open_oneof_branch_is_flagged(tmp_path):
    put(tmp_path, "shared/schemas/events/a.b.schema.json", '{"oneOf": [%s, {"type": "object"}]}' % CLOSED)
    assert problems(check_repo(tmp_path))[0].startswith("shared/schemas/events/a.b.schema.json: schema_not_closed")
def test_event_schema_with_additional_properties_true_or_an_empty_oneof_is_flagged(tmp_path):
    put(tmp_path, "shared/schemas/events/a.b.schema.json", '{"additionalProperties": true}')
    put(tmp_path, "shared/schemas/events/c.d.schema.json", '{"oneOf": []}')
    put(tmp_path, "shared/schemas/events/e.f.schema.json", '[1]')
    assert [p.split(": ")[1] for p in problems(check_repo(tmp_path))] == ["schema_not_closed"] * 3


# T19-R10 output
def test_output_is_sorted_one_line_per_problem_then_the_count(tmp_path):
    put(tmp_path, "z.md", ""); put(tmp_path, "a/b.json", "{"); put(tmp_path, "a/a.md", "TODO")
    r = check_repo(tmp_path)
    assert r.returncode == 1
    assert r.stdout.splitlines() == ["a/a.md: placeholder: the whole content is a placeholder",
                                     "a/b.json: invalid_json: the file is not valid JSON",
                                     "z.md: empty: the file has no bytes", "3 problem(s)"]
def test_output_survives_a_file_name_that_is_not_utf8(tmp_path):
    try:
        (tmp_path / os.fsdecode(b"\xff-name.md")).write_bytes(b"")
    except OSError:
        pytest.skip("this file system refuses names that are not UTF-8")
    r = check_repo(tmp_path, env=dict(os.environ, LC_ALL="C", PYTHONIOENCODING="ascii"))
    assert r.returncode == 1 and "empty" in r.stdout and r.stdout.endswith("1 problem(s)\n"), r.stderr


# T19-R6 the default root
def test_the_root_defaults_to_the_current_directory(tmp_path):
    assert check_repo(cwd=tmp_path).stdout == "0 problem(s)\n"
    put(tmp_path, "a.md", "")
    r = check_repo(cwd=tmp_path)
    assert r.returncode == 1 and problems(r) == ["a.md: empty: the file has no bytes"]


# the walk: what it skips and what it does not follow (a root that is not a git work tree)
@pytest.mark.parametrize("skipped", [".git", ".qa", ".venv39", ".venv", ".tools", ".superpowers", "node_modules", "__pycache__",
                                     "sub/__pycache__", "sub/node_modules", "sub/.venv315"])
def test_the_walk_skips_these_directories(tmp_path, skipped):
    put(tmp_path, skipped + "/empty.md", ""); put(tmp_path, skipped + "/bad.json", "{")
    assert check_repo(tmp_path).returncode == 0
def test_the_walk_reads_qa_config_json_and_nothing_else_under_qa(tmp_path):
    put(tmp_path, ".qa/config.json", "{"); put(tmp_path, ".qa/other.json", "{"); put(tmp_path, ".qa/ledger/x.md", "")
    assert [p.split(": ")[0] for p in problems(check_repo(tmp_path))] == [".qa/config.json"]
def test_a_qa_directory_deeper_down_is_not_special(tmp_path):
    put(tmp_path, "sub/.qa/config.json", "")
    assert problems(check_repo(tmp_path))[0].startswith("sub/.qa/config.json: empty")
def test_the_walk_skips_ds_store(tmp_path):
    put(tmp_path, ".DS_Store", ""); put(tmp_path, "sub/.DS_Store", "")
    assert check_repo(tmp_path).returncode == 0
def test_the_walk_does_not_follow_a_symlink_to_a_directory(tmp_path):
    outside = tmp_path / "outside"; put(outside, "empty.md", "")
    root = tmp_path / "root"; root.mkdir(); (root / "link").symlink_to(outside, target_is_directory=True)
    assert check_repo(root).returncode == 0
def test_irregular_files_are_skipped_not_read(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    (root / "dangling.json").symlink_to(tmp_path / "gone")
    put(tmp_path, "real-empty.md", ""); (root / "to-empty.md").symlink_to(tmp_path / "real-empty.md")
    os.mkfifo(str(root / "fifo.json"))
    r = check_repo(root, timeout=30)   # a checker that opens the FIFO blocks for ever; the timeout turns that into a failure
    assert r.returncode == 0 and r.stdout == "0 problem(s)\n"
def test_a_directory_named_like_a_json_file_is_skipped(tmp_path):
    (tmp_path / "dir.json").mkdir()
    assert check_repo(tmp_path).returncode == 0
@pytest.mark.skipif(not modes_bind(), reason="permission bits do not bind this user")
def test_an_unreadable_regular_file_is_a_problem_not_a_crash(tmp_path):
    p = put(tmp_path, "a.md", "text"); p.chmod(0)
    try:
        r = check_repo(tmp_path)
    finally:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert r.returncode == 1 and problems(r)[0].startswith("a.md: unreadable")


# T19-R4, T19-R5 with a git work tree
@needs_git
def test_a_git_ignored_file_is_not_read(tmp_path):
    git_repo(tmp_path); put(tmp_path, ".gitignore", "ignored/\n*.log\n"); put(tmp_path, "ignored/empty.md", ""); put(tmp_path, "x.log", "")
    put(tmp_path, "tracked.md", "text")
    assert check_repo(tmp_path).stdout == "0 problem(s)\n"
@needs_git
def test_a_tracked_and_an_untracked_file_are_both_read(tmp_path):
    git_repo(tmp_path); put(tmp_path, "tracked.md", ""); git(tmp_path, "add", "tracked.md"); put(tmp_path, "untracked.json", "{")
    assert [p.split(": ")[0] for p in problems(check_repo(tmp_path))] == ["tracked.md", "untracked.json"]
@needs_git
def test_the_walk_would_have_read_what_git_ignores(tmp_path):
    put(tmp_path, ".gitignore", "ignored/\n"); put(tmp_path, "ignored/empty.md", "")
    assert problems(check_repo(tmp_path))[0].startswith("ignored/empty.md: empty")   # no repository here: the walk reads it
@needs_git
def test_a_listed_file_deleted_from_the_tree_is_skipped(tmp_path):
    git_repo(tmp_path); gone = put(tmp_path, "gone.md", "text"); put(tmp_path, "kept.md", "text"); git(tmp_path, "add", "."); gone.unlink()
    r = check_repo(tmp_path)
    assert r.returncode == 0 and r.stdout == "0 problem(s)\n"
@needs_git
def test_a_listed_file_whose_directory_became_a_file_is_skipped(tmp_path):
    git_repo(tmp_path); put(tmp_path, "dir/f.md", "text"); put(tmp_path, "kept.md", "text"); git(tmp_path, "add", ".")
    (tmp_path / "dir" / "f.md").unlink(); (tmp_path / "dir").rmdir(); put(tmp_path, "dir", "now a file")
    r = check_repo(tmp_path)
    assert r.returncode == 0 and r.stdout == "0 problem(s)\n"
@needs_git
def test_a_tracked_symlink_to_a_fifo_is_neither_read_nor_reported(tmp_path):
    git_repo(tmp_path); os.mkfifo(str(tmp_path.parent / (tmp_path.name + ".fifo")))   # outside the work tree; opening it would block
    fifo = tmp_path.parent / (tmp_path.name + ".fifo")
    (tmp_path / "pipe.json").symlink_to(fifo); (tmp_path / "pipe.md").symlink_to(fifo); put(tmp_path, "a.md", "text")
    git(tmp_path, "add", ".")
    r = check_repo(tmp_path, timeout=30)   # a checker that follows the link blocks, and the timeout fails the test without allocating
    assert r.returncode == 0 and r.stdout == "0 problem(s)\n"
@needs_git
def test_a_file_with_a_non_ascii_name_is_listed_and_read(tmp_path):
    git_repo(tmp_path); put(tmp_path, "Programación/vacío.md", "")
    assert problems(check_repo(tmp_path))[0].startswith("Programación/vacío.md: empty")
@needs_git
def test_without_git_on_the_path_the_walk_runs_instead(tmp_path):
    git_repo(tmp_path); put(tmp_path, ".gitignore", "ignored/\n"); put(tmp_path, "ignored/empty.md", "")
    r = check_repo(tmp_path, env={"PATH": str(tmp_path / "no-bin")})
    assert problems(r)[0].startswith("ignored/empty.md: empty")
def test_a_git_that_fails_falls_back_to_the_walk(tmp_path):
    bin_dir = tmp_path / "bin"; fake = put(bin_dir, "git", "#!/bin/sh\nexit 1\n"); fake.chmod(0o755)
    root = tmp_path / "root"; put(root, "a.md", "")
    r = check_repo(root, env={"PATH": str(bin_dir)})
    assert problems(r)[0].startswith("a.md: empty")
def test_a_git_that_prints_false_falls_back_to_the_walk(tmp_path):
    bin_dir = tmp_path / "bin"; fake = put(bin_dir, "git", "#!/bin/sh\necho false\nexit 0\n"); fake.chmod(0o755)
    root = tmp_path / "root"; put(root, "a.md", "")
    assert problems(check_repo(root, env={"PATH": str(bin_dir)}))[0].startswith("a.md: empty")
def test_git_runs_read_only(tmp_path):
    bin_dir = tmp_path / "bin"; log = tmp_path / "args.log"
    fake = put(bin_dir, "git", "#!/bin/sh\necho \"$GIT_OPTIONAL_LOCKS $*\" >> '%s'\ncase \"$*\" in *rev-parse*) echo true;; esac\nexit 0\n" % log)
    fake.chmod(0o755)
    root = tmp_path / "root"; put(root, "a.md", "text")
    check_repo(root, env={"PATH": str(bin_dir)})
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and all(l.startswith("0 -C %s -c core.fsmonitor=false -c core.untrackedCache=false " % root) for l in lines)
    assert "ls-files -z --cached --others --exclude-standard" in lines[1]


# --------------------------------------------------------------------------- the workflow (T19-R11, T19-R12); actionlint is the only other check it gets here

WORKFLOW = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
def test_workflow_pins_every_action_to_a_full_sha_with_its_tag():
    import re
    uses = re.findall(r"^\s*- uses: (\S+)(.*)$", WORKFLOW, re.M)
    assert uses and all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", ref) and re.fullmatch(r" # v\d+\.\d+\.\d+", tag) for ref, tag in uses)
def test_workflow_persists_no_credentials_and_only_checkout_has_that_input():
    import re
    steps = re.split(r"^\s*- (?=uses:)", WORKFLOW, flags=re.M)[1:]
    assert len(steps) == 4
    for step in steps:
        assert ("persist-credentials: false" in step) == step.startswith("uses: actions/checkout@")
def test_workflow_is_read_only_runs_on_push_and_pull_request_and_does_not_fail_fast():
    assert "\npermissions:\n  contents: read\n" in WORKFLOW and "\n  push:\n  pull_request:\n" in WORKFLOW
    assert "fail-fast: false" in WORKFLOW and 'os: [ubuntu-latest, macos-latest]' in WORKFLOW and 'python: ["3.9", "3.15"]' in WORKFLOW
