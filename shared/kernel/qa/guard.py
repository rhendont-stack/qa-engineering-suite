"""Command guard (§12.3): decide whether a command may run, must not run, or needs a grant.

``classify`` reads a command (an argv list, or a shell string with ``shell=True``) and returns
a ``GuardDecision``: ``allow``, ``deny`` or ``require_grant``, the activities the run needs,
the rule that decided, and the hosts of its ``http(s)://`` arguments. It never runs the
command, opens a network connection or writes a file. It reads the rule file, the redaction
patterns and, to follow symbolic links, the paths the command names.

Rules live in ``shared/policies/command-guard.json`` (schema ``command-guard.schema.json``).
Each maps a ``match`` to ``deny``, ``require_grant:<activity>`` or ``allow``, and every matcher
in a ``match`` must hold. Any deny wins, and the first deny rule in file order is reported.
Then require_grant: its activities accumulate after the declared one, and the decision is
``require_grant`` only when at least one was added. Then allow. A command that no rule matches
is allowed with ``rule_id`` None. Its declared activity still goes to authorization, and so
does the environment of every host it names (§12.2).

What the guard reads through:

- the wrappers ``env`` (with its ``NAME=value``, options and ``-S`` string), ``nice``,
  ``nohup``, ``timeout``, ``time``, ``command``, ``exec``, ``stdbuf`` and ``xargs``,
  recursively, and leading ``NAME=value`` assignments;
- ``sh -c`` and ``bash -c`` bodies and ``eval`` arguments, read as shell strings;
- ``find -exec``, ``-execdir``, ``-ok`` and ``-okdir`` commands, with ``{}`` standing for each
  search root;
- shell strings, split with ``shlex`` into simple commands at ``;``, ``&&``, ``||``, ``|``,
  ``&`` and newlines. Bodies of ``$(...)``, backticks, ``<(...)`` and ``>(...)`` are
  classified too, comments are dropped, and output redirection targets are checked like
  the paths a command writes or deletes.

Relative paths, in operands and redirection targets alike, resolve against ``cwd``: the
workspace root when it is omitted, and a relative ``cwd`` is itself read against the root.
The literal ``{evidence}`` token, which the run pipeline replaces after this check, counts as
a directory under ``.qa/evidence/``.

Secrets (§12.5): a secret flag with its value, or a word that a redaction pattern matches, is
denied. An environment assignment whose value only names another variable
(``PGPASSWORD=$PGPASSWORD``) is how a secret should travel, so it is allowed. The same text as
an argument is not, because the shell expands it into the process list.

Limitations, stated plainly. The guard is pattern-based defense in depth, not a sandbox
(§12.3). A script, a test, a configuration file or a tool's own options (``git -c
core.pager=...``, a test runner's plugins) can run a command the guard never sees, and
wrappers and shell constructs not listed above are not read through. Shell variables,
aliases and functions defined elsewhere are not expanded: a program name that comes from a
variable or a substitution is not resolved, and a path that depends on one counts as outside
``.qa/``. A ``cd`` inside a shell string is not followed. A quoted operator such as ``';'``
reads as the operator, and every path operand of a writing command counts as written; both
err towards denial. Real containment comes from the host's permission system and from
running untrusted work in containers.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from . import resource_dir, schema, vocab
from .canonical import loads
from .config import resolve_host
from .errors import QaError
from .redact import load_patterns, shannon_entropy

# QaError codes raised here: guard_policy_invalid, invalid_command, invalid_activity, plus
# invalid_json / schema_invalid from reading and validating the rule file.

_POLICY = "command-guard.json"
_SCHEMA = "command-guard.schema.json"
_MAX_DEPTH = 16       # nested shell strings, substitutions and find -exec commands
_MAX_WRAPPERS = 64    # wrappers stripped from one command

# What a one-element argv must contain to be a shell string passed without --shell.
_SHELL_MARKS = (";", "&&", "||", "|", "&", ">", "<", "$(", "`")
_PUNCT = "();<>|&\n"
_OPERATORS = ("&>>", "<<<", "&&", "||", ";;", "|&", ">>", "<<", "&>", ">&", "<&", ">|", "<>",
              ";", "&", "|", ">", "<", "(", ")", "\n")
_SEPARATORS = frozenset([";", "&&", "||", "|", "|&", "&", ";;", "\n", "(", ")"])
_PIPES = frozenset(["|", "|&"])   # keep commands in the same pipeline; other separators do not
_OUT_REDIRECTS = frozenset([">", ">>", ">|", "&>", "&>>", "<>", ">&"])
_IN_REDIRECTS = frozenset(["<", "<<", "<<<", "<&"])
_KEYWORDS = frozenset(["!", "{", "}", "if", "then", "elif", "else", "fi", "do", "done", "while", "until", "esac"])
_SUBST = "$__QA_SUBST__"   # stands for a substitution's output, which no path check can verify

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Wrapper -> (short options that take a value, long options that take a value, positionals before the command)
_WRAPPERS: Dict[str, Tuple[str, Tuple[str, ...], int]] = {
    "env": ("uCS", ("--unset", "--chdir", "--split-string"), 0),
    "nice": ("n", ("--adjustment",), 0),
    "nohup": ("", (), 0),
    "timeout": ("sk", ("--signal", "--kill-after"), 1),
    "time": ("fo", ("--format", "--output"), 0),
    "command": ("", (), 0),
    "exec": ("a", (), 0),
    "stdbuf": ("ioe", ("--input", "--output", "--error"), 0),
    "xargs": ("adEILnPs", ("--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars",
                           "--process-slot-var"), 0),
}
_SHELLS = frozenset(["sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh"])
_GIT_VALUE_OPTIONS = frozenset(["-C", "-c", "--git-dir", "--work-tree"])

_PRIVILEGE = frozenset(["sudo", "doas", "su", "pkexec", "run0", "sudoedit"])
_DELETERS = frozenset(["rm", "rmdir", "unlink"])
_WRITERS = frozenset(["cp", "mv", "ln", "install", "rsync", "tee", "touch", "truncate", "mkdir", "rm", "rmdir",
                      "unlink", "chmod", "chown", "chgrp", "dd", "sed", "perl", "curl", "wget"])
_IN_PLACE = re.compile(r"^(-[A-Za-z]*i|--in-place)")
_DEVICES = re.compile(r"^/dev/(null|stdout|stderr|tty|fd/[0-9]+)$")
_EVIDENCE = "{evidence}"
_PROTECTED = tuple((tuple(e.rstrip("/").lower().split("/")), e.endswith("/")) for e in vocab.PROTECTED_PATHS)

_SECRET_FLAGS = ("--password", "--token", "--secret", "--api-key")
_VAR_REF = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})$")
# Redaction patterns that match a key=value assignment; for these the key must name a secret
# (see _is_whole_secret_key). Other kinds match a secret value directly and are unchanged.
_ASSIGNMENT_SECRET_KINDS = frozenset(["password_assignment", "secret_assignment"])
# A key word that, on its own, names a secret.
_SECRET_WORDS = frozenset(["pass", "pwd", "password", "passwords", "passwd",
                           "secret", "secrets", "token", "tokens",
                           "apikey", "apikeys", "credential", "credentials"])
# A full keyword a one-word key may end with (``pgpassword``, ``clientsecret``, ``apikey``);
# bare ``pass``/``pwd`` are left out, so ``bypass`` and ``compass`` do not count.
_SECRET_SUFFIXES = ("password", "passwords", "passwd", "secret", "secrets", "token", "tokens",
                    "apikey", "apikeys", "privatekey", "privatekeys", "credential", "credentials")
# A trailing key word that names a file or path, so its value is a reference, not a secret.
_FILE_WORDS = frozenset(["file", "files", "path", "paths", "dir", "directory", "filename", "location", "keyfile"])
_URL = re.compile(r"(?i)\bhttps?://[^\s'\"<>`|;&,(){}\\]+")

# A concrete database connection string (postgres://..., mysql://..., not an http(s) URL).
_INLINE_DSN = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|sqlite|sqlserver|mssql|jdbc|oracle"
                         r"|mongodb|redis|clickhouse|cockroach(?:db)?|snowflake|duckdb|cassandra|db2)"
                         r"(?:\+[a-z0-9]+)?://")
_HEREDOC = re.compile(r"<<-?\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SQL_DESTRUCTIVE = re.compile(r"(?i)\b(?:DROP\s+\w|TRUNCATE\b)")
_SQL_UNBOUNDED = re.compile(r"(?i)\bDELETE\s+FROM\b|\bUPDATE\s+\S+(?:\s+\S+)?\s+SET\b")
_SQL_WHERE = re.compile(r"(?i)\bWHERE\b")
# Migration tools, found anywhere in argv (so npx, bundle exec or python manage.py count), and their verbs.
_MIGRATION_VERBS = (
    (("alembic",), r"upgrade|downgrade|stamp"),
    (("prisma",), r"migrate\s+(?:deploy|dev|reset|resolve)|db\s+(?:push|execute)"),
    (("rails", "rake"),
     r"db:(?:migrate|rollback|reset|drop|setup|prepare|schema:load|seed|purge|truncate_all)(?::(?!status\b)\S+)?"),
    (("flyway",), r"migrate|clean|baseline|repair|undo"),
    (("liquibase",), r"update(?:-count|-to-tag|Count|ToTag)?|rollback(?:-count|-to-date|Count|ToDate)?|drop-all|dropAll"),
    (("manage.py", "django-admin"), r"migrate|flush"),
    (("artisan",), r"migrate(?::\S+)?|db:wipe"),
    (("knex",), r"migrate:(?:latest|up|down|rollback)"),
    (("sequelize", "sequelize-cli"), r"db:migrate(?::undo(?::all)?)?|db:drop"),
    (("typeorm",), r"migration:(?:run|revert)|schema:(?:sync|drop)"),
    (("dbmate",), r"up|migrate|down|rollback|drop"),
    (("drizzle-kit",), r"push|migrate|drop"),
)
_MIGRATIONS = {name: re.compile(r"(?:^|\s)(?:%s)(?=\s|$)" % verbs) for names, verbs in _MIGRATION_VERBS for name in names}

_RULES: Optional[dict] = None
_COMPILED: Optional[List["_Rule"]] = None
_PATTERN_RX: Dict[str, "re.Pattern[str]"] = {}


@dataclass(frozen=True)
class GuardDecision:
    decision: str                          # allow | deny | require_grant
    required_activities: Tuple[str, ...]   # the declared activity first, then every one a rule added
    rule_id: Optional[str]
    reason: str
    hosts: Tuple[str, ...]


@dataclass(frozen=True)
class _Rule:
    id: str
    effect: str                  # deny | allow | require_grant
    activity: Optional[str]      # the activity require_grant names
    reason: str
    match: Dict[str, Any]
    args_rx: Optional["re.Pattern[str]"]
    shell_rx: Optional["re.Pattern[str]"]


@dataclass
class _Unit:
    """One simple command to match rules against."""

    words: List[str]                 # as written, wrappers and assignments included
    argv: List[str]                  # what runs once wrappers are stripped; empty when nothing runs
    redirects: List[str] = field(default_factory=list)   # output redirection targets
    shell: Optional[str] = None      # the whole shell string the command came from
    assignments: List[str] = field(default_factory=list)  # NAME=value words that set its environment
    pipeline: Optional[str] = None   # the text of this command's own pipeline, for the SQL and DSN checks

    @property
    def program(self) -> Optional[str]:
        return os.path.basename(self.argv[0]) if self.argv else None


@dataclass(frozen=True)
class _Context:
    ws_root: str
    qa_dir: str
    cwd: str          # where relative paths resolve
    config: dict
    shell_without_flag: bool


_Hit = Tuple[str, Optional[str]]   # (detail for the reason, activity that replaces the rule's own)


# --------------------------------------------------------------------------- rules

def load_rules() -> dict:
    """``command-guard.json``, validated against its schema once (cached; callers must not mutate it)."""
    global _RULES, _COMPILED
    if _RULES is None:
        doc = loads((resource_dir("policies") / _POLICY).read_bytes())
        schema.validate(doc, _SCHEMA)
        _COMPILED = _compile_rules(doc)
        _RULES = doc
    return _RULES


def _compile_rules(doc: dict) -> List[_Rule]:
    rules: List[_Rule] = []
    seen = set()
    for item in doc["rules"]:
        rid = item["id"]
        if rid in seen:
            raise QaError("guard_policy_invalid", "duplicate rule id", rule=rid)
        seen.add(rid)
        effect, _, activity = item["effect"].partition(":")
        if effect == "require_grant" and activity not in vocab.ACTIVITIES:
            raise QaError("guard_policy_invalid", "require_grant names an unknown activity", rule=rid, activity=activity)
        m = item["match"]
        rules.append(_Rule(rid, effect, activity or None, item["reason"], m,
                           _regex(m.get("args_regex"), rid), _regex(m.get("shell_regex"), rid)))
    return rules


def _regex(pattern: Optional[str], rid: str) -> Optional["re.Pattern[str]"]:
    if pattern is None:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise QaError("guard_policy_invalid", "regex does not compile", rule=rid, error=str(exc))


def _compiled_rules() -> List[_Rule]:
    load_rules()
    assert _COMPILED is not None
    return _COMPILED


# --------------------------------------------------------------------------- classify

def classify(command: Union[List[str], str], *, shell: bool, ws_root: Path, qa_dir: Path,
             config: dict, declared_activity: str, cwd: Optional[Path] = None) -> GuardDecision:
    """Decide ``command``. Relative paths in it resolve against ``cwd``: ``ws_root`` when None,
    and a relative ``cwd`` is itself read against ``ws_root``."""
    if declared_activity not in vocab.ACTIVITIES:
        raise QaError("invalid_activity", "unknown activity", activity=declared_activity)
    texts = _command_texts(command)
    rules = _compiled_rules()
    without_flag = not shell and (isinstance(command, str) or (len(texts) == 1 and any(m in texts[0] for m in _SHELL_MARKS)))
    root = os.path.realpath(str(ws_root))
    where = root if cwd is None else os.path.realpath(os.path.join(root, str(cwd)))
    ctx = _Context(root, os.path.realpath(str(qa_dir)), where, config, without_flag)
    if shell or without_flag:
        # A shell string passed without --shell is still read as the shell string it is.
        units = _shell_units(" ".join(texts), 0, lenient=without_flag)
    else:
        units = _expand(texts, None, [], 0, lenient=False)
    return _decide(rules, units, ctx, declared_activity, tuple(_hosts(texts)))


def _command_texts(command: Union[Sequence[str], str]) -> List[str]:
    texts = [command] if isinstance(command, str) else command
    if (not isinstance(texts, (list, tuple)) or not texts or not all(isinstance(t, str) for t in texts)
            or texts == [""]):
        raise QaError("invalid_command", "a command is a non-empty argv list or a non-empty string")
    if any("\0" in t for t in texts):
        raise QaError("invalid_command", "a command cannot hold a NUL character")
    return list(texts)


def _decide(rules: List[_Rule], units: List[_Unit], ctx: _Context, declared: str,
            hosts: Tuple[str, ...]) -> GuardDecision:
    hits: List[Tuple[_Rule, _Hit]] = []
    for rule in rules:
        for unit in units:
            hit = _match(rule, unit, ctx)
            if hit is not None:
                hits.append((rule, hit))
    for rule, hit in hits:
        if rule.effect == "deny":
            return GuardDecision("deny", (declared,), rule.id, _reason(rule, hit), hosts)
    required = [declared]
    first: Optional[Tuple[_Rule, _Hit]] = None
    for rule, hit in hits:
        if rule.effect == "require_grant":
            activity = hit[1] or rule.activity
            if activity is not None and activity not in required:
                required.append(activity)
                first = first or (rule, hit)
    if first is not None:
        return GuardDecision("require_grant", tuple(required), first[0].id, _reason(*first), hosts)
    chosen = next((h for h in hits if h[0].effect == "allow"), None) or next(iter(hits), None)
    if chosen is None:
        return GuardDecision("allow", (declared,), None,
                             "No rule matched; the declared activity goes to authorization.", hosts)
    reason = _reason(*chosen)
    if chosen[0].effect == "require_grant":
        reason += " The declared activity covers it."
    return GuardDecision("allow", (declared,), chosen[0].id, reason, hosts)


def _reason(rule: _Rule, hit: _Hit) -> str:
    detail = hit[0]
    return rule.reason if not detail else "%s %s." % (rule.reason, detail[0].upper() + detail[1:])


def _match(rule: _Rule, unit: _Unit, ctx: _Context) -> Optional[_Hit]:
    m = rule.match
    prog = unit.program
    if "program" in m and (prog is None or not any(fnmatch.fnmatchcase(prog, p) for p in m["program"])):
        return None
    if m.get("no_args") and len(unit.argv) != 1:
        return None
    args = unit.argv[1:]
    if "subcommand" in m or "unless_subcommand" in m:
        sub, args = _subcommand(unit.argv)
        if sub is None or sub not in m.get("subcommand", (sub,)) or sub in m.get("unless_subcommand", ()):
            return None
    if "args_all" in m and not all(a in args for a in m["args_all"]):
        return None
    if "args_any" in m and not any(a in args for a in m["args_any"]):
        return None
    if "unless_args" in m and any(a in args for a in m["unless_args"]):
        return None
    if rule.args_rx is not None and not any(rule.args_rx.search(a) for a in args):
        return None
    if rule.shell_rx is not None and (unit.shell is None or not rule.shell_rx.search(unit.shell)):
        return None
    check = m.get("check")
    return _CHECKS[check](ctx, unit) if check else ("", None)


def _subcommand(argv: List[str]) -> Tuple[Optional[str], List[str]]:
    """The first argument that is not an option, and the arguments after it."""
    skip = _GIT_VALUE_OPTIONS if argv and os.path.basename(argv[0]) == "git" else frozenset()
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in skip:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, argv[i + 1:]
    return None, []


# --------------------------------------------------------------------------- reading commands

def _expand(words: List[str], shell_text: Optional[str], redirects: List[str], depth: int,
            lenient: bool, pipeline: Optional[str] = None) -> List[_Unit]:
    """The unit for ``words`` plus the units of any command it runs (sh -c, eval, find -exec)."""
    if depth > _MAX_DEPTH:
        raise QaError("invalid_command", "the command nests other commands too deeply")
    argv, assignments = _strip_wrappers(words)
    unit = _Unit(list(words), argv, list(redirects), shell_text, assignments, pipeline)
    units = [unit]
    prog = unit.program
    if prog in _SHELLS:
        body = _shell_body(argv)
        if body is not None:
            units.extend(_shell_units(body, depth + 1, lenient))
    elif prog == "eval" and len(argv) > 1:
        units.extend(_shell_units(" ".join(argv[1:]), depth + 1, lenient))
    elif prog == "find":
        roots, _, commands = _find_parts(argv)
        for inner in commands:
            for root in roots:
                units.extend(_expand([w.replace("{}", root) for w in inner], shell_text, [], depth + 1, lenient, pipeline))
    return units


def _strip_wrappers(words: List[str]) -> Tuple[List[str], List[str]]:
    """What runs once wrappers are stripped, and the NAME=value words that set its environment."""
    argv = list(words)
    assignments: List[str] = []
    for _ in range(_MAX_WRAPPERS):
        while argv and _ASSIGNMENT.match(argv[0]):
            assignments.append(argv[0])
            argv = argv[1:]
        name = os.path.basename(argv[0]) if argv else ""
        if name not in _WRAPPERS:
            return argv, assignments
        argv = _unwrap(name, argv)
    raise QaError("invalid_command", "the command nests too many wrappers")


def _unwrap(name: str, argv: List[str]) -> List[str]:
    """``argv`` without the wrapper ``name``, its options and positionals (env's NAME=value stay for the caller)."""
    short, long_opts, positionals = _WRAPPERS[name]
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            opt, eq, value = a.partition("=")
            separate = opt in long_opts and not eq
            if separate:
                value = argv[i + 1] if i + 1 < len(argv) else ""
            if name == "env" and opt == "--split-string":
                return ["env"] + _split(value) + argv[i + (2 if separate else 1):]
            i += 2 if separate else 1
            continue
        if a.startswith("-") and len(a) > 1:
            letters = a[1:]
            if name == "command" and ("v" in letters or "V" in letters):
                return []   # command -v and -V look a name up; nothing runs
            step = 1
            for j, letter in enumerate(letters):
                if letter in short:
                    attached = letters[j + 1:]
                    if name == "env" and letter == "S":
                        value = attached or (argv[i + 1] if i + 1 < len(argv) else "")
                        return ["env"] + _split(value) + argv[i + (1 if attached else 2):]
                    step = 1 if attached else 2
                    break
            i += step
            continue
        break
    return argv[i + positionals:]


def _split(value: str) -> List[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _shell_body(argv: List[str]) -> Optional[str]:
    """The command string of ``sh -c STRING`` (any of _SHELLS), or None."""
    with_c = False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if a in ("--rcfile", "--init-file"):
            i += 2
            continue
        if a[:1] in ("-", "+") and len(a) > 1:
            letters = "" if a.startswith("--") else a[1:]
            with_c = with_c or (a[0] == "-" and "c" in letters)
            i += 2 if ("o" in letters or "O" in letters) else 1
            continue
        break
    return argv[i] if with_c and i < len(argv) else None


def _find_parts(argv: List[str]) -> Tuple[List[str], bool, List[List[str]]]:
    """``find``'s search roots, whether it has ``-delete``, and its -exec family commands."""
    i = 1
    while i < len(argv) and (argv[i] in ("-H", "-L", "-P", "-D") or argv[i].startswith("-O")):
        i += 2 if argv[i] == "-D" else 1
    roots = []
    while i < len(argv) and not argv[i].startswith(("-", "(", "!", ")")):
        roots.append(argv[i])
        i += 1
    rest = argv[i:]
    commands = []
    j = 0
    while j < len(rest):
        if rest[j] in ("-exec", "-execdir", "-ok", "-okdir"):
            k = j + 1
            while k < len(rest) and rest[k] not in (";", "+"):
                k += 1
            commands.append(rest[j + 1:k])
            j = k + 1
        else:
            j += 1
    return roots or ["."], "-delete" in rest, commands


def _shell_units(text: str, depth: int, lenient: bool) -> List[_Unit]:
    if depth > _MAX_DEPTH:
        raise QaError("invalid_command", "the command nests shell strings too deeply")
    outer, bodies = _substitutions(_extract_heredocs(text))
    rows = _simple_commands(outer, lenient)
    # Commands joined by a pipe share a pipeline; the SQL and DSN checks read only their own.
    # A here-string or here-document body (inputs) belongs to its command's pipeline, not another.
    pipeline_words: Dict[int, List[str]] = {}
    for words, _redirects, pid, inputs in rows:
        pipeline_words.setdefault(pid, []).extend(words + inputs)
    pipeline_text = {pid: " ".join(w) for pid, w in pipeline_words.items()}
    units: List[_Unit] = []
    for words, redirects, pid, _inputs in rows:
        units.extend(_expand(words, text, redirects, depth, lenient, pipeline_text[pid]))
    for body in bodies:
        units.extend(_shell_units(body, depth + 1, lenient))
    # A string with no command still gets one unit, so whole-command checks run on it.
    return units or [_Unit([], [], [], text)]


def _substitutions(text: str) -> Tuple[str, List[str]]:
    """``text`` with each $(...), backtick, <(...) and >(...) body cut out, and comments dropped."""
    out: List[str] = []
    bodies: List[str] = []
    quote = ""
    word_start = True
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            out.append(c)
            quote = "" if c == "'" else quote
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            word_start = False
            continue
        if c == "#" and not quote and word_start:
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("$(", i) or (c in "<>" and not quote and text.startswith("(", i + 1)):
            end = _closing_paren(text, i + 2)
            bodies.append(text[i + 2:end])
        elif c == "`":
            end = _closing_backtick(text, i + 1)
            bodies.append(text[i + 1:end])
        else:
            if c == "'" and not quote:
                quote = "'"
            elif c == '"':
                quote = "" if quote else '"'
            out.append(c)
            word_start = not quote and (c.isspace() or c in _PUNCT)
            i += 1
            continue
        out.append(_SUBST)
        i = end + 1
        word_start = False
    return "".join(out), bodies


def _closing_paren(text: str, start: int) -> int:
    """Index of the ``)`` that closes the parenthesis opened before ``start``, or len(text)."""
    depth, quote, i = 1, "", start
    while i < len(text):
        c = text[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def _closing_backtick(text: str, start: int) -> int:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    return len(text)


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _extract_heredocs(text: str) -> str:
    """Rewrite each here-document (``psql <<EOF`` ... ``EOF``) as a here-string on the command's own
    line, so its body stays with the command for the SQL and DSN checks instead of being read as
    separate commands. The body lines and the closing delimiter are removed."""
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HEREDOC.search(line)
        if m is None:
            out.append(line)
            i += 1
            continue
        dash = line[m.start():m.start() + 3] == "<<-"
        delim = m.group(2)
        body: List[str] = []
        j = i + 1
        while j < len(lines):
            if (lines[j].lstrip("\t") if dash else lines[j]) == delim:
                break
            body.append(lines[j])
            j += 1
        out.append(line[:m.start()] + "<<< " + _shell_quote(" ".join(body)) + line[m.end():])
        i = j + 1   # skip the body and the closing delimiter
    return "\n".join(out)


def _strip_fd_designators(text: str) -> str:
    """Remove a bare file-descriptor number glued to the front of a redirect operator
    (``2>file``, ``1>&2``), outside quotes, so the fd is not read as a command word. A space
    before the number (``echo 2 >file``) leaves it an ordinary argument, as the shell does."""
    out: List[str] = []
    quote = ""
    boundary = True   # at the start of a word (after whitespace, an operator or the start)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            boundary = False
            i += 2
            continue
        if c in "'\"":
            quote = c
            out.append(c)
            boundary = False
            i += 1
            continue
        if boundary and c.isdigit():
            j = i
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] in "<>":
                i = j        # drop the fd digits; the redirect operator keeps the boundary
                continue
        out.append(c)
        boundary = c.isspace() or c in _PUNCT
        i += 1
    return "".join(out)


def _simple_commands(text: str, lenient: bool) -> List[Tuple[List[str], List[str], int, List[str]]]:
    """(words, output redirection targets, pipeline index, here-string/here-doc bodies) for each
    simple command of a shell string. Commands joined by a pipe share a pipeline index; other
    separators start a new one. A ``<<<`` body is kept so the SQL and DSN checks can read it."""
    stripped = _strip_fd_designators(text)
    lex = shlex.shlex(stripped, posix=True, punctuation_chars=_PUNCT)
    lex.whitespace = " \t\r"   # newlines separate commands
    lex.whitespace_split = True
    lex.commenters = ""        # _substitutions dropped the comments; a '#' inside a word stays
    try:
        tokens = list(lex)
    except ValueError:
        if not lenient:
            raise QaError("invalid_command", "the shell string does not parse")
        tokens = stripped.split()
    commands: List[Tuple[List[str], List[str], int, List[str]]] = []
    words: List[str] = []
    redirects: List[str] = []
    inputs: List[str] = []
    pending = ""
    pid = 0
    for tok in _split_operators(tokens):
        if pending:
            if pending in _OUT_REDIRECTS and not (pending == ">&" and (tok.isdigit() or tok == "-")):
                redirects.append(tok)
            elif pending == "<<<":
                inputs.append(tok)
            pending = ""
        elif tok in _SEPARATORS:
            _flush(commands, words, redirects, pid, inputs)
            words, redirects, inputs = [], [], []
            if tok not in _PIPES:
                pid += 1
        elif tok in _OUT_REDIRECTS or tok in _IN_REDIRECTS:
            pending = tok
        else:
            words.append(tok)
    _flush(commands, words, redirects, pid, inputs)
    return commands


def _split_operators(tokens: List[str]) -> List[str]:
    """Split runs of punctuation that shlex returns as one token (``);``, ``&&(``) into operators."""
    out: List[str] = []
    for tok in tokens:
        if tok and all(c in _PUNCT for c in tok):
            i = 0
            while i < len(tok):
                op = next(o for o in _OPERATORS if tok.startswith(o, i))
                out.append(op)
                i += len(op)
        else:
            out.append(tok)
    return out


def _flush(commands: List[Tuple[List[str], List[str], int, List[str]]], words: List[str],
           redirects: List[str], pid: int, inputs: List[str]) -> None:
    i = 0
    while i < len(words):
        if words[i] in _KEYWORDS:
            i += 1
        elif words[i] == "function":
            i += 2
        elif words[i] in ("for", "case", "select"):
            i = len(words)   # a loop or case header runs nothing itself
        else:
            break
    if words[i:] or redirects or inputs:
        commands.append((words[i:], redirects, pid, inputs))


def _hosts(texts: List[str]) -> List[str]:
    hosts: List[str] = []
    for text in texts:
        for m in _URL.finditer(text):
            try:
                host = urllib.parse.urlsplit(m.group(0)).hostname
            except ValueError:
                continue
            if host and host not in hosts:
                hosts.append(host)
    return hosts


# --------------------------------------------------------------------------- paths

def _shell_dependent(path: str) -> bool:
    return path.startswith("~") or any(c in path for c in "$`{}")


def _locations(ctx: _Context, path: str) -> Optional[Tuple[str, str]]:
    """``path`` as written and with symbolic links followed, both absolute; None if the shell decides it."""
    if path == _EVIDENCE or path.startswith(_EVIDENCE + "/"):
        rest = path[len(_EVIDENCE):]
        if _shell_dependent(rest):
            return None
        joined = os.path.join(ctx.qa_dir, "evidence", "EXE-pending") + rest
    elif _shell_dependent(path):
        return None
    else:
        joined = os.path.join(ctx.cwd, path)
    return os.path.normpath(joined), os.path.realpath(joined)


def _inside(path: str, root: str) -> bool:
    return path != root and os.path.commonpath([path, root]) == root


def _inside_qa(ctx: _Context, path: str) -> bool:
    locations = _locations(ctx, path)
    return locations is not None and all(_inside(p, ctx.qa_dir) for p in locations)


def _protected(ctx: _Context, path: str) -> bool:
    """Whether ``path`` is agent or IDE configuration (vocab.PROTECTED_PATHS, any depth, any case)."""
    locations = _locations(ctx, path)
    if locations is None:
        candidates = [os.path.normpath(path)]
    else:
        candidates = [os.path.relpath(p, ctx.ws_root) if p == ctx.ws_root or _inside(p, ctx.ws_root) else p
                      for p in locations]
    for candidate in candidates:
        segments = [s.lower() for s in candidate.replace("\\", "/").split("/") if s not in ("", ".")]
        for entry, is_dir in _PROTECTED:
            n = len(entry)
            for k in range(len(segments) - n + 1):
                if tuple(segments[k:k + n]) == entry and (is_dir or k + n == len(segments)):
                    return True
    return False


def _operands(argv: List[str]) -> List[str]:
    """Arguments that are not options; everything after ``--`` counts."""
    out: List[str] = []
    rest = False
    for a in argv[1:]:
        if rest or not a.startswith("-") or a == "-":
            out.append(a)
        elif a == "--":
            rest = True
    return out


def _write_targets(argv: List[str]) -> List[str]:
    """Paths a writing command may write: operands, ``--opt=value`` values and dd's ``of=``."""
    out: List[str] = []
    for a in argv[1:]:
        if a.startswith("of="):
            out.append(a[3:])
        elif a.startswith("--") and "=" in a:
            out.append(a.split("=", 1)[1])
        elif not a.startswith("-"):
            out.append(a)
    return out


# --------------------------------------------------------------------------- named checks

def _check_delete_outside_qa(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    targets = [t for t in unit.redirects if not _DEVICES.match(os.path.normpath(t))]
    prog = unit.program
    if prog in _DELETERS:
        operands = _operands(unit.argv)
        if not operands:
            return ("%s names no path, so its targets cannot be checked" % prog, None)
        targets.extend(operands)
    elif prog == "find":
        roots, deletes, _ = _find_parts(unit.argv)
        if deletes:
            targets.extend(roots)
    for target in targets:
        if not _inside_qa(ctx, target):
            where = "depends on the shell" if _locations(ctx, target) is None else "is outside .qa/"
            return ("%s %s" % (target, where), None)
    return None


def _check_protected_config_write(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    targets = list(unit.redirects)
    prog = unit.program
    if prog in _WRITERS and (prog not in ("sed", "perl") or any(_IN_PLACE.match(a) for a in unit.argv[1:])):
        targets.extend(_write_targets(unit.argv))
    for target in targets:
        if _protected(ctx, target):
            return ("%s is agent or IDE configuration" % target, None)
    return None


def _check_shell_without_flag(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    return ("a shell string was passed without --shell", None) if ctx.shell_without_flag else None


def _check_secret_in_argv(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    # The program's arguments first, then the wrappers and assignments around them. An
    # assignment whose value only names another variable passes the secret through the
    # environment, as §12.5 asks, so it is skipped; the same text as an argument still counts.
    # The detail names the flag or the pattern kind, never the value.
    by_name = {a for a in unit.assignments if _VAR_REF.match(a.split("=", 1)[1])}
    for words, skip in ((unit.argv, frozenset()), (unit.words, by_name)):
        for i, word in enumerate(words):
            if word in skip:
                continue
            for flag in _SECRET_FLAGS:
                if (word.startswith(flag + "=") and len(word) > len(flag) + 1) or (
                        word == flag and i + 1 < len(words) and words[i + 1] and not words[i + 1].startswith("-")):
                    return ("%s carries its value on the command line" % flag, None)
            kind = _secret_kind(word)
            if kind is not None:
                return ("an argument matches the %s pattern" % kind, None)
    return None


def _secret_kind(text: str) -> Optional[str]:
    """The kind of the first redaction pattern that finds a secret value in ``text``, as Task 4's
    Redactor applies it. §12.5 is the authority (T9-R4): the check keeps secret values out of
    argv, so an assignment pattern counts only when its key is a whole secret keyword carrying a
    value, not when a keyword is a substring of another word (``--bypass-cache``, ``--passthrough``)
    or names a file (``--password-file``, ``PGPASSFILE``). Value patterns (provider tokens, a URL's
    credentials) are unchanged."""
    for p in load_patterns():
        rx = _PATTERN_RX.get(p.regex)
        if rx is None:
            rx = _PATTERN_RX[p.regex] = re.compile(p.regex)
        for m in rx.finditer(text):
            secret = m.group(p.group)
            if secret is None or len(secret) < p.min_len:
                continue
            if p.min_entropy is not None and shannon_entropy(secret) < p.min_entropy:
                continue
            if p.kind in _ASSIGNMENT_SECRET_KINDS and not _is_whole_secret_key(text[:m.start(p.group)]):
                continue
            return p.kind
    return None


def _is_whole_secret_key(key_text: str) -> bool:
    """Whether the key before the value is a secret key carrying a secret (T9-R4). The key is split
    on non-letters into words. It counts when a word is exactly a secret keyword (so ``SECRET_KEY``,
    ``DB_PASSWORD`` and ``MYSQL_PASS`` do), or the key's letters end in a full keyword (so
    ``PGPASSWORD``, ``CLIENT_SECRET`` and ``API_KEY`` do). It does not when the keyword is only part
    of an unrelated word (``bypass``, ``passthrough``) or when the key names a file or path
    (``password-file``, ``PGPASSFILE``, ``token-file``), whose value is a reference, not a secret."""
    words = re.sub(r"[^A-Za-z]+", " ", key_text).lower().split()
    if not words or words[-1] in _FILE_WORDS:
        return False
    if any(w in _SECRET_WORDS for w in words):
        return True
    return "".join(words).endswith(_SECRET_SUFFIXES)


def _check_url_host(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    undeclared = [h for h in _hosts(unit.argv[1:]) if resolve_host(ctx.config, h) is None]
    return ("no environment declares %s" % ", ".join(undeclared), None) if undeclared else None


def _check_privilege_escalation(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    return ("%s escalates privileges" % unit.program, None) if unit.program in _PRIVILEGE else None


def _scan_texts(unit: _Unit) -> List[str]:
    """The text the SQL and DSN checks read: this command's own pipeline, or its argv when it
    came as an argv list. A separator other than a pipe bounds the pipeline, so a DSN named in
    one command does not reach another."""
    return [unit.pipeline] if unit.pipeline is not None else unit.argv[1:]


def _all_dsns_ephemeral(ctx: _Context, texts: List[str]) -> bool:
    """Whether the command targets a database and every database it names is ephemeral. An
    inline connection string (``postgres://host/db``) names a concrete, undeclared database, so
    it counts as production (§12.2) and the answer is then no."""
    if any(_INLINE_DSN.search(t) for t in texts):
        return False
    classes = set()
    for env in ctx.config.get("environments", []):
        name = env.get("dsn_env")
        if name and any(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(name), t) for t in texts):
            classes.add(env.get("class"))
    return classes == {"ephemeral"}


def _destructive_sql(text: str) -> bool:
    for statement in text.split(";"):
        if _SQL_DESTRUCTIVE.search(statement):
            return True
        if _SQL_UNBOUNDED.search(statement) and not _SQL_WHERE.search(statement):
            return True
    return False


def _check_destructive_sql(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    # In a shell string the SQL may reach the client through a pipe within the same pipeline.
    texts = _scan_texts(unit)
    if not any(_destructive_sql(t) for t in texts) or _all_dsns_ephemeral(ctx, texts):
        return None
    return ("the SQL drops, truncates or changes rows without WHERE", None)


def _check_migration(ctx: _Context, unit: _Unit) -> Optional[_Hit]:
    argv = unit.argv
    for i, word in enumerate(argv):
        tool = os.path.basename(word)
        rx = _MIGRATIONS.get(tool)
        if rx is not None and rx.search(" ".join(argv[i + 1:])):
            texts = _scan_texts(unit)
            return ("%s migrates a database" % tool, "db-ephemeral" if _all_dsns_ephemeral(ctx, texts) else None)
    return None


_CHECKS: Dict[str, Callable[[_Context, _Unit], Optional[_Hit]]] = {
    "delete_outside_qa": _check_delete_outside_qa,
    "protected_config_write": _check_protected_config_write,
    "shell_without_flag": _check_shell_without_flag,
    "secret_in_argv": _check_secret_in_argv,
    "url_host": _check_url_host,
    "privilege_escalation": _check_privilege_escalation,
    "destructive_sql": _check_destructive_sql,
    "migration": _check_migration,
}
