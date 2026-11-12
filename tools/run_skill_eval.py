"""Runs one skill evaluation case headless in an isolated, budgeted copy of a fixture.

Usage: python -I tools/run_skill_eval.py --case PATH --config with-skill|baseline --model MODEL --budget-usd N --out DIR
       [--timeout SECONDS] [--ledger PATH] [--keep-workdir]

The case is a JSON file: ``{"id", "prompt", "fixture", "skill"?, "bash_allow"?, "runtime_venv"?, "system_path_only"?, "exclude"?,
"keep_history"?, "checkout"?}``. ``fixture`` and ``skill`` are paths, relative ones resolved against the repository root. The harness
copies the fixture into a fresh temporary directory without answer keys or any SKILL.md, gives the copy a one-commit git history of
its own
(or, with ``keep_history``, the generator's), places only the skill under test in ``.claude/skills/`` (``with-skill`` only),
runs the agent there, and appends one record per run to ``DIR/costs.jsonl``. It never reads, prints or records the environment it
passes to the agent.

Confinement, stated plainly: tool patterns do not fully confine an agent that can run shell commands. The harness gives the
child a minimal environment (``PATH`` and a per-run temporary ``HOME`` and ``CLAUDE_CONFIG_DIR``; no key),
turns off user and local settings, hooks, auto-memory, keychain authentication, MCP servers and (for baseline) slash
commands, and always passes a settings file that denies reading the repository, any ``benchmark`` directory, the real
home and the key helper's directory. Real confinement depends on the CLI's sandbox, which is always required (``failIfUnavailable``), with no network
domains allowed. ``--bare`` is not used: it removes the Skill tool and the skill listing, so a skill could never be found by
its description. Bash is limited to read-only prefixes plus the narrow prefixes a case
lists in ``bash_allow``; never allow a bare interpreter there. The ``--allowedTools`` list is the Bash allow-list: the settings
turn off ``sandbox.autoAllowBashIfSandboxed`` (being sandboxed approves nothing by itself), turn off
``sandbox.allowUnsandboxedCommands`` (no unsandboxed retry) and set ``permissions.denyInsteadOfPrompting`` (an unanswered
prompt is a denial). The sandbox then confines what an allowed command can reach.

``setup_script`` (optional case field) is a repository-relative path to a Python file inside the case's own skill ``evals/``
directory (the case must name its ``skill``); a path anywhere else, a symlink out of that directory or a file that is not a
``.py`` file is refused with exit 2. After the fresh git history is made and before the budget is read or the agent starts,
the harness runs it as ``python -I SCRIPT`` with the copy as working directory, a ``PATH`` and a per-run empty ``HOME`` and
nothing else in its environment (so no API key), a timeout of ``SETUP_TIMEOUT`` seconds and no need for the network. The
script is never copied into the copy, so the agent cannot see it. It may build a ``.qa/`` state through the kernel and damage a
file on purpose for a tamper scenario. A nonzero exit, a timeout or a crash is a refused run (exit 3) that writes no ledger
line and spends nothing. Its stdout and stderr, redacted, are saved as ``DIR/runs/ID/setup.txt`` in every case, refused or not.

A fixture that holds a ``generate.py`` is generated into the copy, never copied: the harness imports it and calls
``generate(directory)``, which writes the files and is never itself placed in the copy. Such a copy gets the one-commit history of
every copy, unless the case says ``keep_history: true``. The harness then calls ``generate_history(directory, checkout=None)``
instead, and keeps the repository the generator made: it must hold one branch with ``HEAD`` on it, no remote and no tag, or the run
is refused with exit 2. ``checkout`` (an integer of 0 or more, only with ``keep_history``) is the index of the commit ``HEAD`` is
left at; later commits are not in the copy at all, because the generator stops there. ``exclude`` (optional case field) is a list
of repository-relative paths, files or directories of the fixture, removed from the copy before any history is made, so an excluded
file shows in no commit and not in ``git status``; a path the copy does not hold, an absolute path or one with ``..`` is refused
with exit 2 (so is any path with a ``..`` component). ``exclude`` cannot be combined with ``keep_history``.

``skills`` and ``agents`` (optional case fields, ``with-skill`` only) install a whole set together, for a skill that works
with others and delegates to subagents. ``skills`` is a list of skill directories (each holding a SKILL.md) copied to the
copy's ``.claude/skills/`` beside the single ``skill``, which keeps working; ``agents`` is a list of ``.md`` files copied to
``.claude/agents/``. Every entry is a repository-relative path whose real path is inside the repository; a symlink anywhere in
it (the path, a parent below the repository, or anything inside a skill directory) is refused with exit 2. The ``Task`` tool is
allowed, in either configuration so the two arms differ only by what is installed, only when the case declares ``agents``.

``runtime_venv`` (optional case field) is a directory inside the repository, such as ``.venv39``, holding the interpreter and
packages the project's tests need. When present its ``bin`` is put first on the child's PATH and the directory is added to
``sandbox.filesystem.allowRead``, which re-opens that one path inside the denied repository; it is added to nothing else. A
path outside the repository is refused.

``system_path_only`` (optional case field, a boolean) makes the child's ``PATH`` ``/usr/bin:/bin:/usr/sbin:/sbin``, with the
``runtime_venv`` ``bin`` first when one is given, so no scanner installed on the host is on the agent's ``PATH``.

The CLI runs with ``--output-format stream-json --verbose``. The ``init`` event gives ``api_key_source`` and the skills and
tools listed; the final ``result`` event gives the metrics. A run whose ``api_key_source`` is not ``HELPER_KEY_SOURCE`` (or
that has no ``init`` event) is recorded with status ``failed`` and its cost, whatever else happened. ``HELPER_KEY_SOURCE`` is
the value the CLI reports when it takes its key from an ``apiKeyHelper`` (measured: ``apiKeyHelper``); if it were ``None`` no
source would be accepted.

The budget authority is one ledger (``--ledger``, default ``.superpowers/eval-ledger/costs.jsonl`` under the repository root),
read under an exclusive lock before every run, whatever ``--out`` is. A run writes a ``start`` line before it begins and an
``end`` line the moment it finishes, before any evidence file; a ``start`` with no ``end`` (a run in flight, or one that died)
refuses further runs, as does an unknown cost. ``DIR/costs.jsonl`` repeats the ``end`` lines for convenience.

After the ``end`` line, the working copy's ``.qa/`` is copied to ``DIR/runs/ID/qa-state/`` so record criteria can be graded
once the temporary working copy is gone. ``redaction.key``, ``authority.key`` and any ``work`` directory are left out, symlinks
are not followed, and the API key and the copy's kernel keys are redacted from every copied file. A failed copy is reported as a problem (exit 1), never
as a crash, and never touches the ledger. ``.qa/work/patches/`` is the one part of ``work`` that is kept.

The copy's final file state is saved beside it, in ``DIR/runs/ID/file-state/``: ``status.txt`` (``git status --porcelain=v1
-uall``), ``diff.patch`` (``git diff --binary`` of tracked and untracked files against the commit the agent started from, so a
commit the agent made still shows; ignored files are left out) and ``meta.json`` (that base commit, the final HEAD, whether the
diff was cut at ``FILE_STATE_LIMIT`` bytes, the installed skills' SKILL.md sha256, ``fixture_tree``, the base commit's tree,
``git_config``, the configuration entries the top-level git process read, ``gitlinks``, ``redacted_files``, and ``error``).
What is enforced, and tested: git never reads the copy's own repository configuration, which the agent could have changed:
``.git`` is copied outside the copy and given a minimal configuration of the harness's, with no user or system configuration,
hooks, fsmonitor, filters, diff drivers or prompts; git's directory, common directory and object directory are pinned to that
copy, replace refs and grafts are off, and the configuration the top-level git process reads is listed before anything else
and must come from the command line and that file alone. That listing covers the top-level process only, so no snapshot call
enters a nested repository (a git started there would read that repository's own configuration): the gitlinks in HEAD, in
the copy's index and in the work tree are recorded in ``gitlinks`` and show in the diff as paths (the diffs compare a tree
with an index, which never enters one), and what lies inside them is not in the state. So
nothing the copy's repository, or a repository nested in it, names runs, and nothing is written into the copy. A file whose
content on either side of the diff holds a kernel key, in any form the redaction knows, is left out of ``diff.patch`` and
named in ``redacted_files``, because git writes a file holding a NUL as a binary patch, which shows no byte of the key as it
is. A ``.git`` that is not a plain directory, holds a symlink, or holds a file that sends git to another
directory (a common-directory redirect, object alternates, grafts) gives no state, only ``meta.json`` with ``error``: a
problem (exit 1), never a crash and never an empty state. The sha256 of each installed SKILL.md, read before the agent
starts, is also in the ledger's ``end`` line (``skill_sha256``, with ``base_commit`` and ``fixture_tree``). The working copy is then deleted, on every path that created it, refused, failed or
interrupted; ``--keep-workdir`` leaves it under the temporary directory, for debugging the harness only.

Redaction replaces the key itself, its hexadecimal form (either case) and its base64 form (standard and URL-safe alphabets,
at each of the three byte alignments, so the key inside a longer encoded stream is caught too) with the marker. The copy's
kernel keys (``.qa/authority.key`` and ``.qa/redaction.key``, read after the setup and again after the agent) are replaced
the same way, and also as a transcript shows a printed binary file (undecodable bytes replaced, JSON-escaped), with
``[REDACTED:kernel-key]``, in the transcript, stderr, setup output, ``qa-state`` and the file state. Out of scope: a key
split across lines or wrapped inside an encoding, or encoded or decoded any other way. The key never enters the agent's environment or
the copy: the harness writes it to a mode-0600 file next to a mode-0700 helper script, in a per-run directory under the
temporary root and outside the copy, and the settings name that script as ``apiKeyHelper`` while denying the directory to the
agent's file tools and to the sandbox. The directory is deleted in the same ``finally`` that deletes the copy.

Escaped processes: ``Sweeper`` polls the process table every 0.5 s from the moment the CLI starts and records every
descendant (pid and start time), polls once more when the CLI has exited, and kills each recorded process still alive with the
same start time, so a grandchild that left the process group (``setsid``) does not outlive the run; a reused pid has another
start time and is left alone. Limits, stated plainly: a process that forks, detaches and whose parent exits between two polls
is re-parented to pid 1 before it is recorded. On Linux the harness makes itself a child subreaper (``PR_SET_CHILD_SUBREAPER``),
so such orphans re-parent to the harness and the final poll finds them; on macOS and elsewhere there is no subreaper and that
case is NOT covered. The process table comes from ``ps`` (``/proc`` on Linux when ``ps`` cannot run); if neither gives a
table the sweeper is blind, the ledger line says ``sweeper_blind: true`` and a warning goes to stderr, so a count of 0 swept
is then not evidence that nothing escaped. A process that detaches and is killed only by this sweeper is also not guaranteed
to be seen when it is started after the final poll. A fixture tree holding any symlink is refused with exit 2 before anything is copied. A case field
``allow_write: true`` allows ``Edit`` and ``Write`` only inside the copy (``Edit(//<copy>/**)``, never a bare allow) in both
configurations, and adds ``Edit`` and ``Write`` denies for the repository, the real home, any ``benchmark`` directory and the
key helper's directory, the same places the ``Read`` denies cover; a deny wins over an allow.

Exit codes: 0 the run finished and was recorded; 1 it failed or timed out and was recorded; 2 bad arguments; 3 refused
because the budget is reached or a recorded cost is unknown.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import fcntl
import importlib.util
import json
import math
import os
import shutil
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import uuid
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
CONFIGS = ("with-skill", "baseline")
DEFAULT_TIMEOUT = 1800
SETUP_TIMEOUT = 120
SETUP_OUTPUT_LIMIT = 1_000_000
# Narrow on purpose: read and search the copy. No raw git, no editing, network or interpreter tools, no permission
# bypass. A case adds narrow prefixes (for example the skill's launcher) in ``bash_allow``.
BASE_TOOLS = ["Read", "Grep", "Glob", "Bash(ls *)", "Bash(head *)", "Bash(tail *)", "Bash(wc *)"]
FORBIDDEN_BARE = {"python", "python3", "find", "bash", "sh", "zsh", "env", "node", "sudo", "xargs", "eval", "exec"}
KEY_VAR = "ANTHROPIC_API_KEY"
# The ``api_key_source`` the CLI reports when it takes its key from an ``apiKeyHelper``, as measured by the paid probe of
# 2026-11-10 (three runs, every ``init`` event reported this value). While it is None no source is accepted.
HELPER_KEY_SOURCE: Optional[str] = "apiKeyHelper"
WRITE_TOOLS = ["Edit", "Write"]
PR_SET_CHILD_SUBREAPER = 36
SWEEP_INTERVAL = 0.5
MARKER = "[REDACTED:%s]" % KEY_VAR
DEFAULT_LEDGER = REPO / ".superpowers" / "eval-ledger" / "costs.jsonl"
ESSENTIAL_FLAGS = ("--settings", "--setting-sources", "--strict-mcp-config", "--max-budget-usd")
# Never copied from a fixture: answer keys, anything that discovers as a skill, and build or VCS debris.
SKIP_NAMES = {".git", ".claude", "node_modules", "__pycache__", ".pytest_cache", ".venv", ".gradle", "build", "target", "benchmark",
              "SKILL.md"}
SKIP_PREFIXES = ("answer-key", "answer_key")
SKILL_SKIP = {"evals", "__pycache__"}


class Refused(Exception):
    """The run must not start; ``args[0]`` says why. ``output`` is a failed setup script's saved output, when there was one."""

    output: Optional[str] = None


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def now_utc() -> str:
    moment = utcnow()
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def usage_error(message: str) -> "SystemExit":
    print("error: " + message, file=sys.stderr)
    return SystemExit(2)


def resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO / p


def load_case(path: Path) -> Dict[str, Any]:
    try:
        case = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise usage_error("cannot read the case file: %s" % exc)
    if not isinstance(case, dict) or not isinstance(case.get("prompt"), str) or not case["prompt"].strip() \
            or not isinstance(case.get("fixture"), str) or not isinstance(case.get("id"), str):
        raise usage_error("the case needs string fields id, prompt and fixture")
    allow = case.get("bash_allow", [])
    if not (isinstance(allow, list) and all(isinstance(t, str) and t.strip() for t in allow)):
        raise usage_error("bash_allow must be a list of non-empty strings")
    for prefix in allow:
        if prefix.split()[0] in FORBIDDEN_BARE and len(prefix.split()) < 2:
            raise usage_error("bash_allow entry %r is too broad: name the script or subcommand too" % prefix)
    if "allow_write" in case and not isinstance(case["allow_write"], bool):
        raise usage_error("allow_write must be true or false")
    if "keep_history" in case and not isinstance(case["keep_history"], bool):
        raise usage_error("keep_history must be true or false")
    if "checkout" in case and (type(case["checkout"]) is not int or case["checkout"] < 0 or case.get("keep_history") is not True):
        raise usage_error("checkout must be a commit index of 0 or more, and needs keep_history: true")
    if "exclude" in case:
        case["exclude"] = check_exclude(case["exclude"])
        if case.get("keep_history"):
            raise usage_error("exclude cannot be combined with keep_history: a kept history already holds the file")
    script = case.get("setup_script")
    if script is not None:
        case["setup_script"] = str(check_setup_script(case, script))
    for field, kind in (("skills", "dir"), ("agents", "file")):
        if field in case:
            case[field] = check_installables(field, case[field], kind)
    venv = case.get("runtime_venv")
    if "system_path_only" in case and not isinstance(case["system_path_only"], bool):
        raise usage_error("system_path_only must be true or false")
    if venv is not None:
        if not isinstance(venv, str) or not venv.strip():
            raise usage_error("runtime_venv must be a repository-relative directory")
        real, root = os.path.realpath(str(resolve(venv))), os.path.realpath(str(REPO))
        if not real.startswith(root + os.sep) or not os.path.isdir(os.path.join(real, "bin")):
            raise usage_error("runtime_venv must be a directory with a bin inside the repository: %s" % venv)
    return case


def check_exclude(entries: Any) -> List[str]:
    """The ``exclude`` paths, normalised; otherwise exit 2. Each is relative and stays inside the copy."""
    if not isinstance(entries, list) or not entries or not all(isinstance(e, str) and e.strip() and "\x00" not in e for e in entries):
        raise usage_error("exclude must be a non-empty list of repository-relative paths")
    found: List[str] = []
    for entry in entries:
        path = os.path.normpath(entry)
        if os.path.isabs(entry) or ".." in entry.replace("\\", "/").split("/") or path == ".":
            raise usage_error("exclude entry must be a plain relative path, with no '..': %s" % entry)
        if path not in found:
            found.append(path)
    return found


def _has_symlink(root: str, path: str) -> bool:
    """True when ``path`` or any directory between ``root`` and it is a symlink."""
    probe = path
    while len(probe) > len(root) and probe.startswith(root + os.sep):
        if os.path.islink(probe):
            return True
        probe = os.path.dirname(probe)
    return False


def check_installables(field: str, entries: Any, kind: str) -> List[str]:
    """The real paths of a ``skills`` (directories with a SKILL.md) or ``agents`` (.md files) list; otherwise exit 2.

    Each entry must be a repository-relative path, inside the repository once resolved, with no symlink in it."""
    if not isinstance(entries, list) or not entries or not all(isinstance(e, str) and e.strip() and "\x00" not in e for e in entries):
        raise usage_error("%s must be a non-empty list of repository-relative paths" % field)
    root = str(REPO)
    found: List[str] = []
    for entry in entries:
        lexical = os.path.normpath(str(resolve(entry)))
        if not lexical.startswith(root + os.sep):
            raise usage_error("%s entry is not inside the repository: %s" % (field, entry))
        if _has_symlink(root, lexical):
            raise usage_error("%s entry contains a symlink, which is refused: %s" % (field, entry))
        real = lexical
        if kind == "dir":
            if not os.path.isfile(os.path.join(real, "SKILL.md")):
                raise usage_error("%s entry must be a skill directory holding a SKILL.md: %s" % (field, entry))
            for base, dirs, files in os.walk(real, followlinks=False):
                if [n for n in dirs + files if os.path.islink(os.path.join(base, n))]:
                    raise usage_error("%s entry holds a symlink, which is refused: %s" % (field, entry))
        elif not (real.endswith(".md") and os.path.isfile(real)):
            raise usage_error("%s entry must be an existing .md file: %s" % (field, entry))
        if real not in found:
            found.append(real)
    return found


def check_setup_script(case: Dict[str, Any], script: Any) -> Path:
    """The real path of ``script`` when it is a .py file inside the case's own skill ``evals/`` directory; otherwise exit 2."""
    if not isinstance(script, str) or not script.strip() or "\x00" in script:
        raise usage_error("setup_script must be a repository-relative path to a Python file")
    skill = case.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        raise usage_error("setup_script needs the case to name its skill: the script must live in that skill's evals directory")
    evals = os.path.realpath(str(resolve(skill) / "evals"))
    real = os.path.realpath(str(resolve(script)))
    if not real.startswith(evals + os.sep) or not real.endswith(".py") or not os.path.isfile(real):
        raise usage_error("setup_script must be an existing .py file inside the skill's own evals directory: %s" % script)
    return Path(real)


def run_setup(script: str, workdir: Path, home: Path) -> str:
    """Runs the setup script in the copy. Returns its output, the API key and the copy's kernel keys redacted (the keys are
    read while the copy still exists, because a refused setup deletes it); raises ``Refused`` (with ``output``) when it fails.
    The output is redacted as bytes, before any decoding, so no newline translation or replaced byte hides a key."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    key = os.environ.get(KEY_VAR, "")

    def clean(raw: bytes) -> str:
        forms = kernel_key_forms(kernel_keys(workdir))
        text = redact_kernel_keys(redact_bytes(raw, key), forms).decode("utf-8", "replace")
        return redact_kernel_keys_text(redact(text, key), forms)[:SETUP_OUTPUT_LIMIT]

    try:
        proc = subprocess.run([sys.executable, "-I", script], cwd=str(workdir), env=env, capture_output=True,
                              timeout=SETUP_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        refusal = Refused("the setup script ran past %d seconds" % SETUP_TIMEOUT)
        refusal.output = clean(exc.stdout if isinstance(exc.stdout, bytes) else b"")
        raise refusal
    except OSError as exc:
        refusal = Refused("the setup script could not start: %s" % type(exc).__name__)
        refusal.output = ""
        raise refusal
    output = clean(b"exit %d\n--- stdout\n%s--- stderr\n%s" % (proc.returncode, proc.stdout, proc.stderr))
    if proc.returncode != 0:
        refusal = Refused("the setup script exited %d; nothing was spent" % proc.returncode)
        refusal.output = output
        raise refusal
    return output


# -- the copy ---------------------------------------------------------------------------------------------------------

def _ignore(_directory: str, names: List[str]) -> List[str]:
    return [n for n in names if n in SKIP_NAMES or n.startswith(SKIP_PREFIXES)]


def _git_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    stamp = now_utc()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="eval", GIT_AUTHOR_EMAIL="eval@example.invalid",
               GIT_COMMITTER_NAME="eval", GIT_COMMITTER_EMAIL="eval@example.invalid", GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return env


def fresh_history(workdir: Path) -> None:
    """One commit holding the copy, no remote, no tags, nothing from the source repository."""
    env = _git_env()
    for args in (["init", "-q"], ["add", "-A"], ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial import"]):
        subprocess.run(["git", "-C", str(workdir)] + args, check=True, env=env, capture_output=True, timeout=120)


def generate_fixture(fixture: Path, workdir: Path, case: Dict[str, Any]) -> None:
    """Writes only the generator's output into ``workdir``; the generator itself is never copied."""
    keep = case.get("keep_history") is True
    try:
        spec = importlib.util.spec_from_file_location("_fixture_generator", str(fixture / "generate.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        raise usage_error("the generator could not be loaded: %s" % type(exc).__name__)
    if keep and not callable(getattr(module, "generate_history", None)):
        raise usage_error("keep_history needs a generate.py with a generate_history function")
    try:
        if keep:
            module.generate_history(workdir, checkout=case.get("checkout"))
        else:
            module.generate(workdir)
    except SystemExit as exc:
        raise usage_error("the generator refused: %s" % exc.code)
    except Exception as exc:
        raise usage_error("the generator failed: %s" % type(exc).__name__)
    check_generated_tree(workdir)
    if keep:
        check_kept_history(workdir, case.get("checkout"))


def check_generated_tree(workdir: Path) -> None:
    """The generator's output follows the copy rules that matter: no symlink, no answer-key name, no ``.claude`` or ``benchmark``."""
    for base, dirs, files in os.walk(str(workdir), followlinks=False):
        if base == str(workdir) and ".git" in dirs:
            dirs.remove(".git")
        for name in dirs + files:
            if os.path.islink(os.path.join(base, name)):
                raise usage_error("the generator wrote a symlink, which is refused: %s" % name)
            if name.startswith(SKIP_PREFIXES) or name in (".claude", "benchmark"):
                raise usage_error("the generator wrote a name no copy may hold: %s" % name)


def check_kept_history(workdir: Path, checkout: Optional[int] = None) -> None:
    """A kept history is one branch with HEAD on it, no remote and no tag (a tag or any other ref is a ref), a clean work tree and,
    with ``checkout``, exactly ``checkout + 1`` commits; otherwise exit 2."""
    env = _git_env()

    def git(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(["git", "-C", str(workdir)] + list(args), env=env, capture_output=True, text=True, timeout=120)

    refs = git("for-each-ref", "--format=%(refname)")
    head = git("symbolic-ref", "-q", "HEAD")
    if refs.returncode != 0 or head.returncode != 0 or refs.stdout.split() != [head.stdout.strip()] \
            or not head.stdout.startswith("refs/heads/") or git("remote").stdout.strip():
        raise usage_error("the generated history must hold one branch with HEAD on it, no remote and no tag")
    count = git("rev-list", "--count", "--all")
    if git("status", "--porcelain").stdout.strip() or count.returncode != 0 \
            or (checkout is not None and count.stdout.strip() != str(checkout + 1)):
        raise usage_error("the generated history must leave a clean work tree and, with checkout, exactly checkout + 1 commits")


def remove_excluded(workdir: Path, relative: str) -> None:
    target = workdir / relative
    root = os.path.realpath(str(workdir))
    if not os.path.lexists(str(target)) or not os.path.realpath(str(target.parent)).startswith(root):
        raise usage_error("exclude names a path the copy does not hold: %s" % relative)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(str(target))
    else:
        target.unlink()


def build_workdir(case: Dict[str, Any], config: str, setup_home: Optional[Path] = None, setup_log: Optional[List[str]] = None) -> Path:
    fixture = resolve(case["fixture"])
    if not fixture.is_dir():
        raise usage_error("the fixture is not a directory: %s" % fixture)
    for base, dirs, files in os.walk(str(fixture), followlinks=False):
        if [n for n in dirs + files if os.path.islink(os.path.join(base, n))]:
            raise usage_error("the fixture holds a symlink, which is refused: %s" % base)
    skills: List[Path] = []
    if config == "with-skill":
        if not isinstance(case.get("skill"), str) or not (resolve(case["skill"]) / "SKILL.md").is_file():
            raise usage_error("with-skill needs a case whose skill directory holds a SKILL.md")
        skills = [resolve(case["skill"])] + [Path(p) for p in case.get("skills", [])]
    workdir = Path(tempfile.mkdtemp(prefix="qa-eval-"))
    try:
        if (fixture / "generate.py").is_file():
            generate_fixture(fixture, workdir, case)
        elif case.get("keep_history"):
            raise usage_error("keep_history needs a fixture that holds a generate.py")
        else:
            shutil.copytree(str(fixture), str(workdir), ignore=_ignore, dirs_exist_ok=True)
        for relative in case.get("exclude", []):
            remove_excluded(workdir, relative)
        if case.get("keep_history") is not True:
            fresh_history(workdir)
        installed = set()
        for skill in skills:
            if skill.name in installed:
                continue
            installed.add(skill.name)
            shutil.copytree(str(skill), str(workdir / ".claude" / "skills" / skill.name),
                            ignore=lambda _d, names: [n for n in names if n in SKILL_SKIP])
        if skills and case.get("agents"):
            (workdir / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
            for agent in case["agents"]:
                shutil.copyfile(agent, str(workdir / ".claude" / "agents" / Path(agent).name))
        if installed:
            # The installed skills and agents are the harness's, not the project's: not in the commit, not shown as untracked.
            exclude = workdir / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write("\n/.claude/skills/\n/.claude/agents/\n")
        if case.get("setup_script") and setup_home is not None:
            setup_log.append(run_setup(case["setup_script"], workdir, setup_home))
    except BaseException:
        shutil.rmtree(str(workdir), ignore_errors=True)
        raise
    return workdir


# -- the ledger -------------------------------------------------------------------------------------------------------

class Ledger:
    """The budget authority: JSON lines, each with ``event`` of ``start`` or ``end``, guarded by an exclusive file lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")

    def locked(self) -> "_Lock":
        return _Lock(self.lock_path)

    def read(self) -> List[Dict[str, Any]]:
        """Every record, or ``Refused`` if one cannot be read: a damaged ledger cannot prove spend is under budget."""
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if not isinstance(record, dict) or record.get("event") not in ("start", "end") or "run_id" not in record:
                raise Refused("ledger line %d is not readable; a person must look before more runs, because the spend "
                              "is unknown" % number)
            records.append(record)
        return records

    def append(self, record: Dict[str, Any]) -> None:
        append_line(self.path, record)


class _Lock:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o644)

    def __enter__(self) -> "_Lock":
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: Any) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def check_budget(records: Sequence[Dict[str, Any]], budget: float) -> float:
    """Returns the budget left, or raises ``Refused``."""
    ended = {r["run_id"]: r for r in records if r["event"] == "end"}
    for record in records:
        if record["event"] == "start" and record["run_id"] not in ended:
            raise Refused("run %s has started and not finished (it is in flight, or it died); wait for it, or a person "
                          "must look, because its cost is unknown" % record["run_id"])
    for record in ended.values():
        if record.get("cost_unknown") or not _is_amount(record.get("cost_usd")):
            raise Refused("run %s has an unknown cost; a person must look before more runs, because an unknown cost "
                          "cannot be proven under budget" % record["run_id"])
    spent = sum(r["cost_usd"] for r in ended.values())
    if spent >= budget:
        raise Refused("budget reached: %.4f USD recorded against a budget of %.4f" % (spent, budget))
    return budget - spent


def run_cap(remaining: float) -> str:
    """The per-run cap, rounded down to four decimals, so the cap never exceeds what is left."""
    capped = Decimal(repr(remaining)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if capped < Decimal("0.0001"):
        raise Refused("the budget left (%r USD) is below the smallest per-run cap the CLI flag can carry" % remaining)
    return "%.4f" % capped


def _is_amount(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def append_line(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


# -- the CLI ----------------------------------------------------------------------------------------------------------

def probe_env() -> Dict[str, str]:
    """The environment for the CLI's ``--help`` probes: the same minimal set as the agent's, so no key passes."""
    return {"PATH": os.environ.get("PATH", os.defpath), "HOME": os.environ.get("HOME", os.devnull)}


def cli_help(exe: str) -> str:
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=60, env=probe_env())
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout + r.stderr


def plugin_eval_available(exe: str) -> bool:
    """Detected only. Its case layout is not the harness's case file, so the run still goes through ``claude -p``."""
    try:
        return subprocess.run([exe, "plugin", "eval", "--help"], capture_output=True, timeout=60, env=probe_env()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def isolation_plan(help_text: str, config: str, workdir: Path, config_dir: Path, tools: List[str], cap: str,
                   runtime_venv: Optional[str] = None, helper_command: Optional[str] = None,
                   helper_dir: Optional[Path] = None, allow_write: bool = False) -> Dict[str, Any]:
    """Everything that depends on the installed CLI's flags and settings schema, in one place.

    Raises ``Refused`` when an essential flag is missing. Confirmed from the CLI's help and docs: flag names, the ``Bash(prefix *)``
    pattern form, ``//`` for an absolute path in a permission rule, and the ``sandbox`` settings. Only a paid run can confirm
    that the ``--settings`` denies are honored.

    The boundary, precisely: the ``--allowedTools`` list is the Bash allow-list. ``autoAllowBashIfSandboxed: false`` means a
    sandboxed command is not approved merely for being sandboxed, so every Bash command goes through the permission flow and only
    those matching an allow rule (or the CLI's built-in read-only set) run. The sandbox confines what an allowed command can reach.
    ``allowUnsandboxedCommands: false`` turns off unsandboxed retries (the ``dangerouslyDisableSandbox`` parameter is ignored), and
    ``denyInsteadOfPrompting: true`` denies whatever would otherwise wait on a prompt nobody answers. ``runtime_venv`` (an already validated real path) is added to ``allowRead`` only.
    """
    missing = [f for f in ESSENTIAL_FLAGS if f not in help_text]
    if missing:
        raise Refused("the installed claude CLI does not list %s; refusing to run without isolation and a per-run cap"
                      % ", ".join(missing))
    config_dir.mkdir(parents=True, exist_ok=True)
    mcp = config_dir / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    home = os.path.expanduser(os.environ.get("HOME", "~"))
    keep = os.path.realpath(str(workdir))
    denied, deny_read = [], []
    for root in (str(REPO), home):
        real = os.path.realpath(root)
        if real and not keep.startswith(real.rstrip("/") + "/") and real != keep:   # never deny the copy itself
            denied.append("Read(//%s/**)" % real.lstrip("/"))      # a double slash anchors at the filesystem root
            deny_read.append(real)
    if helper_dir is not None:
        real = os.path.realpath(str(helper_dir))
        denied.append("Read(//%s/**)" % real.lstrip("/"))
        deny_read.append(real)
    denied.append("Read(**/benchmark/**)")
    if allow_write:      # a deny wins over an allow: the file tools may write the copy and nothing else listed here
        for rule in list(denied):
            for tool in WRITE_TOOLS:
                denied.append(tool + rule[len("Read"):])
    # Built-in file tools run outside the sandbox, so both the permission denies and the sandbox denies are needed. The
    # sandbox is always required: if the CLI cannot start it, the run fails rather than running unconfined.
    settings: Dict[str, Any] = {
        "permissions": {"deny": denied, "denyInsteadOfPrompting": True},
        "sandbox": {"enabled": True, "failIfUnavailable": True,
                    "autoAllowBashIfSandboxed": False, "allowUnsandboxedCommands": False,
                    "filesystem": {"denyRead": deny_read, "allowRead": [keep] + ([runtime_venv] if runtime_venv else [])},
                    "network": {"allowedDomains": []}}}
    if helper_command:
        settings["apiKeyHelper"] = helper_command
    settings_path = config_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, sort_keys=True), encoding="utf-8")
    args = ["--setting-sources", "project", "--strict-mcp-config", "--mcp-config", str(mcp), "--allowedTools", ",".join(tools),
            "--max-budget-usd", cap]
    args += ["--settings", str(settings_path)]
    slash_off = config == "baseline" and "--disable-slash-commands" in help_text   # with-skill needs its skill invocable
    if slash_off:
        args.append("--disable-slash-commands")
    return {"args": args, "sandbox": True, "bare": False, "slash_commands_off": slash_off}


def build_command(exe: str, model: str, plan: Dict[str, Any]) -> List[str]:
    return [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", model] + plan["args"]


def _find_cat() -> str:
    """The absolute path of ``cat``, resolved when the helper is written so the script never searches a PATH."""
    for candidate in (shutil.which("cat"), "/bin/cat", "/usr/bin/cat"):
        if candidate and os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise Refused("no cat executable was found for the key helper; refusing to run")


def write_key_helper(key: str, helper_dir: Path) -> str:
    """Writes the key to a mode-0600 file and a mode-0700 script that prints it, both in ``helper_dir`` (made mode 0700).

    Returns the ``apiKeyHelper`` command. The directory must be outside the agent's copy; the caller deletes it."""
    cat = _find_cat()
    helper_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(helper_dir), 0o700)
    key_file, script = helper_dir / "key", helper_dir / "helper.sh"
    body = "#!/bin/sh\nexec %s -- %s\n" % (shlex.quote(cat), shlex.quote(str(key_file)))
    for path, mode, text in ((key_file, 0o600, key), (script, 0o700, body)):
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
    return shlex.quote(str(script))


SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def child_env(home: Path, config_dir: Path, path_prefix: Optional[str] = None, system_path_only: bool = False) -> Dict[str, str]:
    """The only environment the agent gets: no key (it comes from the helper), and nothing inherited, so no GIT_* either."""
    path = SYSTEM_PATH if system_path_only else os.environ.get("PATH", os.defpath)
    if path_prefix:
        path = path_prefix + os.pathsep + path
    return {"PATH": path, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(config_dir)}


def key_forms(key: str) -> List[bytes]:
    """The byte strings that would give the key away, longest first: the key, its hex form in either case, and the base64
    core of the key at each byte alignment in the standard and URL-safe alphabets. The core keeps only the characters that
    depend on the key's bytes alone, so it matches the key wherever it sits inside a longer encoded stream."""
    if not key:
        return []
    return sorted(_encoded_forms(key.encode("utf-8")), key=len, reverse=True)


def _encoded_forms(raw: bytes) -> set:
    forms = {raw, raw.hex().encode("ascii"), raw.hex().upper().encode("ascii")}
    bits = 8 * len(raw)
    for shift in range(3):
        start, end = -(-8 * shift // 6), (8 * shift + bits) // 6
        padded = b"\0" * shift + raw + b"\0\0"
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            core = encode(padded)[start:end]
            if len(core) >= 16:
                forms.add(core)
    return forms


KERNEL_KEY_FILES = ("authority.key", "redaction.key")
KERNEL_KEY_MARKER = "[REDACTED:kernel-key]"
KERNEL_KEY_MIN, KERNEL_KEY_MAX = 16, 4096


def kernel_keys(workdir: Path) -> List[bytes]:
    """The contents of the copy's kernel key files (``.qa/authority.key``, ``.qa/redaction.key``): regular files only, never
    through a symlink, between ``KERNEL_KEY_MIN`` and ``KERNEL_KEY_MAX`` bytes. Read after the setup and again after the agent,
    so a key made or replaced during the run is caught too."""
    found: List[bytes] = []
    qa = workdir / ".qa"
    if not qa.is_dir() or qa.is_symlink():
        return found
    for name in KERNEL_KEY_FILES:
        path = qa / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if KERNEL_KEY_MIN <= len(data) <= KERNEL_KEY_MAX and data not in found:
            found.append(data)
    return found


def kernel_key_forms(keys: Sequence[bytes]) -> List[bytes]:
    """The byte strings that would give a kernel key away, longest first: what ``key_forms`` covers for the API key, plus the
    key read as UTF-8 with undecodable bytes replaced, raw and JSON-escaped (with and without ASCII escapes), which is how
    a transcript shows a binary file that was printed. Out of scope: a key split up, or shown any other way."""
    forms: set = set()
    for raw in keys:
        for value in {raw, raw.strip()}:
            if len(value) < KERNEL_KEY_MIN:
                continue
            forms |= _encoded_forms(value)
            text = value.decode("utf-8", "replace")
            for shown in (text, json.dumps(text)[1:-1], json.dumps(text, ensure_ascii=False)[1:-1]):
                if len(shown.encode("utf-8")) >= KERNEL_KEY_MIN:
                    forms.add(shown.encode("utf-8"))
    return sorted(forms, key=len, reverse=True)


def redact_kernel_keys(data: bytes, forms: Sequence[bytes]) -> bytes:
    for form in forms:
        data = data.replace(form, KERNEL_KEY_MARKER.encode("ascii"))
    return data


def redact_kernel_keys_text(text: str, forms: Sequence[bytes]) -> str:
    for form in forms:
        try:
            shown = form.decode("utf-8")
        except UnicodeDecodeError:
            continue                     # raw bytes that are not text cannot sit in a decoded string as they are
        text = text.replace(shown, KERNEL_KEY_MARKER)
    return text


def redact_bytes(data: bytes, key: str) -> bytes:
    for form in key_forms(key):
        data = data.replace(form, MARKER.encode("ascii"))
    return data


def redact(text: str, key: str) -> str:
    if not key:
        return text
    for form in key_forms(key):
        text = text.replace(form.decode("utf-8"), MARKER)
    return text


QA_STATE_SKIP_FILES = {"redaction.key", "authority.key"}
QA_STATE_SKIP_DIRS = {"work"}
QA_STATE_KEEP_IN_WORK = "patches"


def copy_qa_state(workdir: Path, dest: Path, key: str, key_forms_extra: Sequence[bytes] = ()) -> None:
    """Copy the working copy's ``.qa/`` to ``dest``, so a record criterion can be graded after the workdir is gone.

    Left out: ``redaction.key``, ``authority.key`` and any ``work`` directory, except ``.qa/work/patches/`` (the patches a
    skill leaves when it may not write into the project), which is kept. Symlinks are never followed (a link is skipped,
    not copied). The API key and the kernel keys (``key_forms_extra``), if they appear in any file, are replaced by their
    markers. Raises OSError on a failure."""
    source = workdir / ".qa"
    if not source.is_dir() or source.is_symlink():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(str(source), followlinks=False):
        base = Path(root)
        top_work = base == source / "work"
        if top_work:      # only the patches survive from the work directory: no worktree, no scratch file
            dirs[:] = [d for d in dirs if d == QA_STATE_KEEP_IN_WORK and not (base / d).is_symlink()]
            files = []
        else:
            dirs[:] = [d for d in dirs if (d not in QA_STATE_SKIP_DIRS or base == source) and not (base / d).is_symlink()]
        target = dest / base.relative_to(source)
        if not top_work:
            target.mkdir(parents=True, exist_ok=True)
        for name in files:
            path = base / name
            if name in QA_STATE_SKIP_FILES or path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            (target / name).write_bytes(redact_kernel_keys(redact_bytes(data, key), key_forms_extra))


# -- the copy's final file state ----------------------------------------------------------------------------------------

FILE_STATE_LIMIT = 20_000_000       # bytes of diff kept; more is cut and ``diff_truncated`` says so
# Every git call on the copy: no user or system configuration (``_git_env``), and nothing the repository could name runs.
_GIT_SAFE_OPTS = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=" + os.devnull,
                  "-c", "core.attributesFile=" + os.devnull, "-c", "core.quotePath=false", "-c", "protocol.allow=never",
                  # Never into a nested repository: a git started there reads that repository's own configuration.
                  "-c", "submodule.recurse=false", "-c", "diff.ignoreSubmodules=all", "-c", "status.submoduleSummary=false",
                  "-c", "fetch.recurseSubmodules=false"]
_NO_NESTED = "--ignore-submodules=all"
# The diffs compare a tree with an index, which never starts a git inside a nested repository; ``none`` overrides the
# ``diff.ignoreSubmodules`` above and any ``.gitmodules`` setting, so a gitlink the agent left shows in the diff as a path.
_GITLINKS_SHOWN = "--ignore-submodules=none"
_GITLINK_MODE = b"160000"
_CLEAN_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"


class FileStateError(Exception):
    """The file state cannot be taken safely; ``args[0]`` says why."""


def _safe_git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = _git_env()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1")
    env.update(extra or {})
    return env


def base_commit(workdir: Path) -> Optional[str]:
    """The copy's HEAD before the agent starts (the harness made the repository, so its configuration is the harness's)."""
    try:
        done = subprocess.run(["git", "-C", str(workdir)] + _GIT_SAFE_OPTS + ["rev-parse", "--verify", "-q", "HEAD^{commit}"],
                              env=_safe_git_env(), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = done.stdout.strip()
    return head if done.returncode == 0 and len(head) in (40, 64) else None


def base_tree(workdir: Path, base: Optional[str]) -> Optional[str]:
    """The tree of the base commit: the fixture exactly as the harness committed it, so a grader can tell when the fixture in
    the repository is no longer the one the run started from."""
    if base is None:
        return None
    try:
        done = subprocess.run(["git", "-C", str(workdir)] + _GIT_SAFE_OPTS + ["rev-parse", "--verify", "-q", base + "^{tree}"],
                              env=_safe_git_env(), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    tree = done.stdout.strip()
    return tree if done.returncode == 0 and len(tree) in (40, 64) else None


def installed_skill_hashes(workdir: Path) -> Dict[str, str]:
    """``{skill name: "sha256:<hex>"}`` of each SKILL.md installed in the copy, read before the agent starts."""
    import hashlib
    found: Dict[str, str] = {}
    skills = workdir / ".claude" / "skills"
    if skills.is_dir() and not skills.is_symlink():
        for folder in sorted(skills.iterdir()):
            path = folder / "SKILL.md"
            if folder.is_dir() and not folder.is_symlink() and path.is_file() and not path.is_symlink():
                found[folder.name] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return found


# Files inside a git directory that make git read another directory's configuration or objects, or rewrite history: the
# common-directory redirect, object alternates (local and over http) and grafts. The snapshot refuses a copy holding any.
_GIT_INDIRECTIONS = ("commondir", "objects/info/alternates", "objects/info/http-alternates", "info/grafts")


def _plain_git_dir(git_dir: Path) -> None:
    """``git_dir`` is a real directory with no symlink anywhere in it and no indirection file, or ``FileStateError``: a link
    or an indirection could point git (and the harness's own writes) at anything outside the copy, its configuration included."""
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise FileStateError("the copy's .git is not a plain directory")
    for base, dirs, files in os.walk(str(git_dir), followlinks=False):
        if [n for n in dirs + files if os.path.islink(os.path.join(base, n))]:
            raise FileStateError("the copy's .git holds a symlink")
    found = [name for name in _GIT_INDIRECTIONS if os.path.lexists(str(git_dir / name))]
    if found:
        raise FileStateError("the copy's .git holds an indirection file: %s" % ", ".join(found))


def _config_problem(listing: bytes, own_config: Path) -> Tuple[List[str], Optional[str]]:
    """``(entries, problem)`` for ``git config --list --show-origin -z``: each entry ``"<command line|harness> <key>"``. The
    problem names the first entry from anywhere else than the command line or the harness's own clean configuration file, or
    a key that file does not hold; then git would read configuration the agent may control."""
    allowed = {"core.repositoryformatversion", "core.filemode", "core.bare"}
    entries: List[str] = []
    fields = listing.split(b"\0")
    for origin, item in zip(fields[0::2], fields[1::2]):
        where = origin.decode("utf-8", "replace")
        key = item.split(b"\n", 1)[0].decode("utf-8", "replace").lower()
        if where == "command line:":
            entries.append("command line " + key)
        elif where.startswith("file:") and os.path.realpath(where[5:]) == os.path.realpath(str(own_config)) and key in allowed:
            entries.append("harness " + key)
        else:
            return entries, "git would read configuration from %s" % ("another file" if where.startswith("file:") else where)
    return entries, None


def _gitlinks(listing: bytes, sha_field: int) -> List[Dict[str, str]]:
    """The gitlinks (mode 160000) in ``git ls-tree -r -z`` (``sha_field`` 2) or ``git ls-files -s -z`` (1) output."""
    found = []
    for entry in listing.split(b"\0"):
        head, _tab, path = entry.partition(b"\t")
        fields = head.split(b" ")
        if path and fields[0] == _GITLINK_MODE and len(fields) > sha_field:
            found.append({"commit": fields[sha_field].decode("ascii", "replace"), "path": path.decode("utf-8", "replace")})
    return sorted(found, key=lambda item: item["path"])


def _changed_blobs(raw: bytes) -> List[Tuple[bytes, List[str]]]:
    """``(path, [blob ids])`` for each entry of ``git diff --raw -z --no-renames``: the blobs on both sides, absent sides and
    gitlinks left out."""
    fields, changed = raw.split(b"\0"), []
    for header, path in zip(fields[0::2], fields[1::2]):
        parts = header.lstrip(b":").split(b" ")
        if len(parts) < 4:
            continue
        blobs = [parts[2 + side].decode("ascii") for side in (0, 1)
                 if parts[side] != _GITLINK_MODE and parts[2 + side].strip(b"0")]
        changed.append((path, blobs))
    return changed


def _blob_contents(batch: bytes) -> Dict[str, bytes]:
    """The objects in ``git cat-file --batch`` output, by id; a missing one is left out."""
    contents: Dict[str, bytes] = {}
    at = 0
    while at < len(batch):
        end = batch.find(b"\n", at)
        if end < 0:
            break
        header = batch[at:end].split(b" ")
        at = end + 1
        if len(header) == 3 and header[2].isdigit():
            size = int(header[2])
            contents[header[0].decode("ascii")] = batch[at:at + size]
            at += size + 1
    return contents


def capture_file_state(workdir: Path, base: Optional[str], scratch: Path, secrets: Sequence[bytes] = ()) -> Dict[str, Any]:
    """The copy's ``git status --porcelain=v1 -uall`` and ``git diff --binary`` against ``base`` (the commit the agent started
    from), tracked and untracked files alike, ignored files left out.

    What is enforced, and tested: ``.git`` is refused when it holds a symlink or an indirection file; otherwise it is copied
    into ``scratch`` and given a minimal configuration of the harness's; git's directory, common directory and object
    directory are pinned to that copy, replace refs and grafts are off, and the configuration the top-level git process reads
    is listed first and must hold nothing but the command line and that file, so no filter, diff driver, fsmonitor or hook
    the agent may have set in the copy's repository runs, and nothing is written into the copy. No call enters a nested
    repository, because a git started inside one reads that repository's own configuration, which no listing here sees:
    status takes ``--ignore-submodules=all`` and the submodule options in ``_GIT_SAFE_OPTS``, and the diffs compare a tree
    with an index only, so a gitlink shows in them as a path and its commit. The gitlinks in HEAD, in the copy's index and
    in the work tree are also recorded; what lies inside them is not in the state. Untracked files enter
    the diff through a separate index in ``scratch``. A file whose content on either side of the diff holds one of
    ``secrets`` (byte strings) is left out of the diff and named in ``redacted_files``: a file git writes as a binary patch
    shows no byte of a key as it is, so redacting the patch afterwards would miss it.

    Returns ``{status, diff, head_commit, config, gitlinks, redacted_files}`` (bytes, bytes, text, the configuration entries
    as ``"<origin> <key>"``, ``{head, index, work_tree}`` lists of ``{path, commit}``, sorted paths); raises
    ``FileStateError``."""
    if base is None:
        raise FileStateError("the base commit was not read before the agent started")
    git_dir = workdir / ".git"
    _plain_git_dir(git_dir)
    copy = scratch / "git"
    shutil.copytree(str(git_dir), str(copy), symlinks=True)
    for name in ("config", "config.worktree"):
        if os.path.lexists(str(copy / name)):
            os.unlink(str(copy / name))
    (copy / "config").write_text(_CLEAN_CONFIG, encoding="utf-8")
    # Pinned, so no file inside the copied directory can send git elsewhere, and replace refs and grafts change no object.
    where = {"GIT_DIR": str(copy), "GIT_COMMON_DIR": str(copy), "GIT_OBJECT_DIRECTORY": str(copy / "objects"),
             "GIT_WORK_TREE": str(workdir), "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull}

    def git(args: List[Any], index: Optional[Path] = None, given: Optional[bytes] = None) -> bytes:
        env = _safe_git_env(dict(where, **({"GIT_INDEX_FILE": str(index)} if index else {})))
        try:
            done = subprocess.run(["git"] + _GIT_SAFE_OPTS + args, cwd=str(workdir), env=env, capture_output=True, timeout=300,
                                  **({"input": given} if given is not None else {"stdin": subprocess.DEVNULL}))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FileStateError("git %s could not run: %s" % (args[0], type(exc).__name__))
        if done.returncode != 0:
            raise FileStateError("git %s exited %d" % (args[0], done.returncode))
        return done.stdout

    config, problem = _config_problem(git(["config", "--list", "--show-origin", "-z"]), copy / "config")
    if problem is not None:
        raise FileStateError(problem)
    status = git(["status", "--porcelain=v1", "-uall", "--no-renames", _NO_NESTED])
    head = git(["rev-parse", "--verify", "-q", "HEAD^{commit}"]).decode("ascii", "replace").strip()
    gitlinks = {"head": _gitlinks(git(["ls-tree", "-r", "-z", "--full-tree", "HEAD"]), 2),
                "index": _gitlinks(git(["ls-files", "-s", "-z"]), 1)}
    index = scratch / "full-index"
    git(["read-tree", base], index)
    git(["add", "-A", "--", "."], index)
    gitlinks["work_tree"] = _gitlinks(git(["ls-files", "-s", "-z"], index), 1)
    compare = ["--cached", "--no-abbrev", "--no-ext-diff", "--no-textconv", "--no-renames", _GITLINKS_SHOWN, base]
    changed = _changed_blobs(git(["diff", "--raw", "-z"] + compare + ["--"], index))
    wanted = sorted({blob for _path, blobs in changed for blob in blobs})
    contents = _blob_contents(git(["cat-file", "--batch"], given="".join(b + "\n" for b in wanted).encode("ascii"))) \
        if secrets and wanted else {}
    held = sorted(path for path, blobs in changed if any(form in contents.get(blob, b"") for blob in blobs for form in secrets))
    left_out = [":(literal,exclude)" + os.fsdecode(path) for path in held]
    diff = git(["diff", "--binary"] + compare + ["--", "."] + left_out, index)
    return {"status": status, "diff": diff, "head_commit": head, "config": config, "gitlinks": gitlinks,
            "redacted_files": [path.decode("utf-8", "replace") for path in held]}


def save_file_state(workdir: Path, dest: Path, base: Optional[str], skills: Dict[str, str], key: str,
                    fixture_tree: Optional[str] = None, key_forms_extra: Sequence[bytes] = ()) -> Optional[str]:
    """Writes ``status.txt``, ``diff.patch`` and ``meta.json`` under ``dest``, redacted (the API key and the kernel keys).
    Returns None, or the problem: then only ``meta.json`` is written, with ``error``, so a grader reading the snapshot finds no
    state rather than an empty one. ``meta.json`` also holds ``fixture_tree`` (the base commit's tree), ``git_config``
    (the configuration git read while taking the state), ``gitlinks`` (nested repositories, recorded and never entered) and
    ``redacted_files`` (the files left out of the diff because they hold a kernel key)."""
    dest.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {"base_commit": base, "head_commit": None, "head_moved": None, "skill_sha256": skills,
                            "diff_truncated": None, "diff_bytes": None, "error": None, "fixture_tree": fixture_tree,
                            "git_config": None, "gitlinks": None, "redacted_files": None}
    scratch = Path(tempfile.mkdtemp(prefix="qa-eval-state-"))
    try:
        try:
            state = capture_file_state(workdir, base, scratch, key_forms_extra)
        except (FileStateError, OSError, shutil.Error) as exc:
            meta["error"] = str(exc) if isinstance(exc, FileStateError) else type(exc).__name__
        else:
            diff = redact_kernel_keys(redact_bytes(state["diff"], key), key_forms_extra)   # redacted whole, then cut
            (dest / "status.txt").write_bytes(redact_kernel_keys(redact_bytes(state["status"], key), key_forms_extra))
            (dest / "diff.patch").write_bytes(diff[:FILE_STATE_LIMIT])
            meta.update(head_commit=state["head_commit"], head_moved=state["head_commit"] != base,
                        diff_truncated=len(diff) > FILE_STATE_LIMIT, diff_bytes=len(diff), git_config=state["config"],
                        gitlinks=state["gitlinks"], redacted_files=state["redacted_files"])
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)
    (dest / "meta.json").write_text(json.dumps(meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return None if meta["error"] is None else meta["error"]


def _number(value: Any) -> Optional[float]:
    return value if _is_amount(value) else None


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_events(stdout: str) -> List[Dict[str, Any]]:
    """The JSON objects of a ``stream-json`` run, one per line; a line that is not an object is skipped."""
    events: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def extract_metrics(stdout: str) -> Dict[str, Any]:
    """The only place that knows the CLI's JSON field names. A missing or malformed field is None, never zero.

    The metrics come from the last ``result`` event; ``api_key_source``, ``skills`` and ``tools`` from the ``init`` event.
    """
    events = parse_events(stdout)
    data = next((e for e in reversed(events) if e.get("type") == "result"), {})
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    skills, tools = init.get("skills"), init.get("tools")
    source = init.get("apiKeySource")
    return {"cost_usd": _number(data.get("total_cost_usd")), "duration_ms": _number(data.get("duration_ms")),
            "is_error": data.get("is_error") is True,
            "api_key_source": source if isinstance(source, str) else None,
            "skills": [x for x in skills if isinstance(x, str)] if isinstance(skills, list) else None,
            "skill_tool": ("Skill" in tools) if isinstance(tools, list) else None,
            "tokens": {"input": _count(usage.get("input_tokens")), "output": _count(usage.get("output_tokens")),
                       "cache_read": _count(usage.get("cache_read_input_tokens")),
                       "cache_creation": _count(usage.get("cache_creation_input_tokens"))}}


def _proc_table() -> Dict[int, Tuple[int, str]]:
    """The same table from ``/proc`` (Linux); the start time is the kernel's tick count, which is all a comparison needs."""
    table: Dict[int, Tuple[int, str]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "r", encoding="utf-8", errors="replace") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            table[int(name)] = (int(fields[1]), "t" + fields[19])
        except (OSError, IndexError, ValueError):
            continue
    return table


def process_table() -> Dict[int, Tuple[int, str]]:
    """pid -> (ppid, start time) for every process, from ``ps``, or ``/proc`` when ``ps`` cannot run; empty when neither works."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
    try:
        done = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,lstart="], capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return _proc_table()
    table: Dict[int, Tuple[int, str]] = {}
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), " ".join(parts[2].split()))
    return table or _proc_table()


def become_subreaper() -> bool:
    """Linux only: orphans of the CLI's descendants re-parent to this process instead of pid 1, so a final poll finds them."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError, ImportError):
        return False


class Sweeper:
    """Records every descendant of ``root_pid`` (pid and start time) while the run lasts, then kills the ones still alive.

    A process that leaves the process group (``setsid``) survives ``killpg``; it is still a descendant while its parent lives,
    so polling often enough sees it. A pid reused later has a different start time and is never killed."""

    def __init__(self, root_pid: int, extra_roots: Sequence[int] = ()) -> None:
        self.root = root_pid
        self.roots = [root_pid] + [r for r in extra_roots if r != root_pid]
        self.blind = False
        self.seen: Dict[Tuple[int, str], bool] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def poll(self) -> None:
        table = process_table()
        if not table:
            self.blind = True       # a live system always has this process in its table
        children: Dict[int, List[int]] = {}
        for pid, (ppid, _start) in table.items():
            children.setdefault(ppid, []).append(pid)
        stack, visited = list(self.roots), set(self.roots)
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in visited:
                    visited.add(child)
                    self.seen[(child, table[child][1])] = True
                    stack.append(child)

    def _loop(self) -> None:
        while True:
            self.poll()
            if self._stop.wait(SWEEP_INTERVAL):
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=30)

    def kill_survivors(self) -> List[int]:
        """SIGKILLs each recorded process still alive with the recorded start time; returns the pids it killed."""
        current, killed = process_table(), []
        for pid, start in list(self.seen):
            if pid in (0, 1, os.getpid()) or pid not in current or current[pid][1] != start:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except OSError:
                pass
        return killed


def _kill_group(proc: "subprocess.Popen[str]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        proc.kill()


def execute(argv: List[str], prompt: str, workdir: Path, timeout: int, env: Dict[str, str]) -> Dict[str, Any]:
    """Runs the CLI in its own process group, so a timeout or interrupt kills its children too."""
    started = utcnow()
    subreaper = become_subreaper()
    outcome: Dict[str, Any] = {"timed_out": False, "interrupted": False, "exit_code": None, "stdout": "", "stderr": "", "swept": [], "sweeper_blind": False,
                               "subreaper": subreaper}
    pending: Optional[BaseException] = None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=str(workdir), env=env, start_new_session=True)
    except OSError as exc:
        outcome["stderr"] = "could not start the CLI: %s" % exc
        outcome["wall_ms"] = 0
        return outcome
    sweeper = Sweeper(proc.pid, (os.getpid(),) if outcome["subreaper"] else ())
    sweeper.start()
    try:
        out, err = proc.communicate(prompt, timeout=timeout)
        outcome.update(exit_code=proc.returncode, stdout=out, stderr=err)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        sweeper.stop()
        sweeper.poll()
        outcome["swept"] = sweeper.kill_survivors()        # an escaped process may hold the pipes open
        out, err = proc.communicate()
        outcome.update(timed_out=True, stdout=out or "", stderr=err or "")
    except BaseException as exc:  # KeyboardInterrupt, SIGTERM, anything: stop the agent and say so
        _kill_group(proc)
        proc.wait()
        outcome.update(interrupted=True, stderr="interrupted: %s" % type(exc).__name__)
        pending = exc
    finally:
        sweeper.stop()
        sweeper.poll()                                     # once more now that the CLI is gone: orphans may have re-parented
        outcome["sweeper_blind"] = sweeper.blind
        outcome["swept"] = sorted(set(outcome["swept"]) | set(sweeper.kill_survivors()))
    outcome["wall_ms"] = int((utcnow() - started).total_seconds() * 1000)
    outcome["pending"] = pending
    return outcome


# -- main -------------------------------------------------------------------------------------------------------------

def parse(argv: Sequence[str]) -> argparse.Namespace:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> None:  # argparse exits 2 already; keep the message on stderr only
            print("error: " + message, file=sys.stderr)
            raise SystemExit(2)

    p = Parser(description="Run one skill evaluation case headless.")
    p.add_argument("--case", required=True)
    p.add_argument("--config", required=True, choices=CONFIGS)
    p.add_argument("--model", required=True)
    p.add_argument("--budget-usd", required=True, type=float)
    p.add_argument("--out", required=True)
    p.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--keep-workdir", action="store_true", help="leave the working copy under the temporary directory (debugging)")
    args = p.parse_args(list(argv))
    if not (math.isfinite(args.budget_usd) and args.budget_usd > 0) or args.timeout <= 0:
        raise usage_error("--budget-usd and --timeout must be positive numbers")
    return args


def save_setup_output(run_dir: Path, output: Optional[str], key: str) -> None:
    """A refused setup keeps its output as evidence (``run_setup`` has already redacted the copy's kernel keys, while the copy
    existed); a failure to save it is said on stderr, never a crash."""
    if not output:
        return
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "setup.txt").write_text(redact(output, key), encoding="utf-8")
    except OSError as exc:
        print("setup output not saved: %s" % type(exc).__name__, file=sys.stderr)


def refuse(message: str) -> int:
    print(json.dumps({"refused": message}, sort_keys=True))
    print("refused: %s" % message, file=sys.stderr)
    return 3


def _on_sigterm(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt()


def main(argv: Sequence[str]) -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)       # before anything is created, so a SIGTERM always reaches a ``finally``
    args = parse(argv)
    case = load_case(Path(args.case))
    out, ledger = Path(args.out), Ledger(Path(args.ledger))
    key = os.environ.get(KEY_VAR, "")
    if not key:
        return refuse("%s is not set. Evaluation runs are paid with an API key from that variable; set it yourself, the "
                      "harness never asks for, stores or creates one" % KEY_VAR)
    exe = shutil.which("claude")
    if exe is None:
        raise usage_error("the claude CLI is not on PATH")
    help_text = cli_help(exe)
    missing = [f for f in ESSENTIAL_FLAGS if f not in help_text]
    if missing:
        return refuse("the installed claude CLI does not list %s; refusing to run without isolation and a per-run cap"
                      % ", ".join(missing))

    tools = list(BASE_TOOLS) + ["Bash(%s *)" % b.strip() for b in case.get("bash_allow", [])]
    if args.config == "with-skill":
        tools.append("Skill")
    if case.get("agents"):
        tools.append("Task")
    venv = case.get("runtime_venv")
    venv_real = os.path.realpath(str(resolve(venv))) if venv else None
    run_id = uuid.uuid4().hex[:12]
    setup_home, setup_log = Path(tempfile.mkdtemp(prefix="qa-eval-setup-")), []
    try:
        workdir = build_workdir(case, args.config, setup_home, setup_log)
    except Refused as exc:
        save_setup_output(out / "runs" / run_id, exc.output, key)
        return refuse(str(exc))
    finally:
        shutil.rmtree(str(setup_home), ignore_errors=True)
    base, skills = base_commit(workdir), installed_skill_hashes(workdir)     # before the agent can change either
    tree, early_keys = base_tree(workdir, base), kernel_keys(workdir)       # the setup's keys, before the agent can change them
    if case.get("allow_write") is True:       # the allow covers the copy only; the settings deny the rest
        scope = os.path.realpath(str(workdir)).lstrip("/")
        tools += ["%s(//%s/**)" % (tool, scope) for tool in WRITE_TOOLS]
    helper_dir: Optional[Path] = None
    try:
        helper_dir = Path(tempfile.mkdtemp(prefix="qa-eval-key-"))   # outside the copy, deleted with it, whichever way this ends
        helper_command = write_key_helper(key, helper_dir)
        return _run_in(args, case, key, exe, help_text, tools, venv_real, run_id, workdir, out, ledger, setup_log,
                       helper_command, helper_dir, base, skills, tree, early_keys)
    except Refused as exc:
        return refuse(str(exc))
    finally:
        if helper_dir is not None:
            shutil.rmtree(str(helper_dir), ignore_errors=True)
        if not args.keep_workdir:
            shutil.rmtree(str(workdir), ignore_errors=True)


def _run_in(args: argparse.Namespace, case: Dict[str, Any], key: str, exe: str, help_text: str, tools: List[str],
            venv_real: Optional[str], run_id: str, workdir: Path, out: Path, ledger: "Ledger", setup_log: List[str],
            helper_command: str, helper_dir: Path, base: Optional[str] = None, skills: Optional[Dict[str, str]] = None,
            tree: Optional[str] = None, early_keys: Sequence[bytes] = ()) -> int:
    """Everything after the working copy exists; ``main`` deletes the copy whichever way this ends."""
    home = Path(tempfile.mkdtemp(prefix="qa-eval-home-"))
    try:
        try:
            with ledger.locked():
                cap = run_cap(check_budget(ledger.read(), args.budget_usd))
                plan = isolation_plan(help_text, args.config, workdir, home / "config", tools, cap, venv_real,
                                      helper_command, helper_dir, case.get("allow_write") is True)
                ledger.append({"event": "start", "run_id": run_id, "timestamp": now_utc(), "case_id": case["id"],
                               "config": args.config, "model": args.model})
        except Refused as exc:
            return refuse(str(exc))
        command = build_command(exe, args.model, plan)
        outcome = execute(command, case["prompt"], workdir, args.timeout, child_env(home, home / "config", os.path.join(venv_real, "bin") if venv_real else None,
                                                                case.get("system_path_only") is True))
    finally:
        shutil.rmtree(str(home), ignore_errors=True)
    if outcome["sweeper_blind"]:
        print("warning: the sweeper could not read the process table; 0 swept does not show that nothing escaped", file=sys.stderr)
    pending = outcome.pop("pending", None)
    secrets = kernel_key_forms(list(early_keys) + [k for k in kernel_keys(workdir) if k not in early_keys])
    stdout = redact_kernel_keys_text(redact(outcome["stdout"], key), secrets)
    stderr = redact_kernel_keys_text(redact(outcome["stderr"], key), secrets)
    metrics = extract_metrics(outcome["stdout"])
    cost = metrics["cost_usd"]
    if outcome["interrupted"]:
        status, cost = "interrupted", None
    elif outcome["timed_out"]:
        status = "timeout"
    elif outcome["exit_code"] != 0 or metrics["is_error"]:
        status = "failed"
    else:
        status = "ok"
    if (HELPER_KEY_SOURCE is None or metrics["api_key_source"] != HELPER_KEY_SOURCE) and status != "interrupted":
        status = "failed"          # the run was not paid with the helper's key, so it proves nothing
    record = {"event": "end", "run_id": run_id, "timestamp": now_utc(), "case_id": case["id"], "config": args.config,
              "model": args.model, "engine": "claude -p", "plugin_eval_available": plugin_eval_available(exe),
              "command": command[1:], "prompt_via": "stdin", "isolation": {k: v for k, v in plan.items() if k != "args"},
              "workdir": str(workdir), "status": status, "exit_code": outcome["exit_code"],
              "timed_out": outcome["timed_out"], "cost_usd": cost, "cost_unknown": cost is None,
              "duration_ms": metrics["duration_ms"], "wall_ms": outcome["wall_ms"], "tokens": metrics["tokens"],
              "api_key_source": metrics["api_key_source"], "swept_pids": len(outcome["swept"]), "sweeper_blind": outcome["sweeper_blind"], "skills_listed": metrics["skills"], "skill_tool_present": metrics["skill_tool"],
              "skill_sha256": skills or {}, "base_commit": base, "fixture_tree": tree}
    try:                                       # first the ledger line, before anything that can fail
        ledger.append(record)
    except OSError as exc:
        print("LEDGER WRITE FAILED (%s); the record follows so the spend is not lost:\n%s"
              % (exc, json.dumps(record, sort_keys=True)), file=sys.stderr)
        return 1
    problems = []
    run_dir = out / "runs" / run_id
    try:
        if Path(args.out, "costs.jsonl").resolve() != ledger.path.resolve():
            append_line(out / "costs.jsonl", record)
        run_dir.mkdir(parents=True, exist_ok=True)
        if setup_log:
            (run_dir / "setup.txt").write_text(redact_kernel_keys_text(redact(setup_log[0], key), secrets), encoding="utf-8")
        (run_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    except OSError as exc:
        problems.append("evidence not fully saved: %s" % exc)
        print(problems[-1] + " (the ledger line was written)", file=sys.stderr)
    try:                                       # after the ledger line; a failed copy never undoes the run
        copy_qa_state(workdir, run_dir / "qa-state", key, secrets)
    except Exception as exc:                   # noqa: BLE001 - whatever goes wrong here is a problem, not a crash
        problems.append("qa-state not saved: %s" % type(exc).__name__)
        print(problems[-1] + " (the ledger line was written)", file=sys.stderr)
    try:                                       # the copy's final file state, before the copy is deleted
        failure = save_file_state(workdir, run_dir / "file-state", base, skills or {}, key, tree, secrets)
    except Exception as exc:                   # noqa: BLE001 - as for qa-state: a problem, never a crash
        failure = type(exc).__name__
    if failure is not None:
        problems.append("file state not saved: %s" % redact(failure, key))
        print(problems[-1] + " (the ledger line was written)", file=sys.stderr)
    print(json.dumps({"run_id": run_id, "status": status, "cost_usd": cost, "cost_unknown": cost is None,
                      "workdir": str(workdir), "evidence": str(run_dir)}, sort_keys=True))
    if isinstance(pending, KeyboardInterrupt):
        return 130
    if isinstance(pending, BaseException):
        raise pending
    return 0 if status == "ok" and not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
