"""CI environments (§12.2 "Unattended CI"): pinning a CI job to the accepted configuration, and writing that job.

``begin`` (``qa ci begin``) runs first in the job, on a fresh checkout. It records ``ci.begun``, which makes the workspace
CI-pinned whatever it found, and, only when the checked-out ``.qa/config.json`` hashes to the hash the workflow carries,
``config.accepted`` with basis ``ci-pin``. A CI-pinned workspace may use only activities confined to the runner, records no
grants, changes no configuration and computes no verdict (``authz``, ``config``, ``gate``); a ``ci`` environment can be used
nowhere else. Nothing reads ``CI=true``: only these events say a workspace is a CI job's.

``generate`` (``qa ci generate``) writes, as a patch under ``.qa/work/patches/`` and never into the project, a GitHub Actions
workflow that re-runs recorded host runs at their commit and uploads the evidence bundle (§5.2, §22.2 #10). The job runs the
launcher the project tracks at that commit, so a re-run is the same kernel the project pinned, not one the runner installs.

What the workflow cannot carry, it refuses rather than escapes: an argument, report pattern or path holding ``${{`` (GitHub
evaluates it in any ``run`` text, before the shell sees it) or a line break or other control character (a line break ends
the block scalar the command sits in). Everything else is quoted for the shell with ``shlex.quote``.
"""
from __future__ import annotations

import math
import os
import posixpath
import re
import shlex
import stat
import string
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import config, gitinfo, integrity, ledger, redact, resource_dir, schema, state, vocab, workspace
from .canonical import loads, sha256_bytes
from .errors import QaError
from .workspace import Workspace

# QaError codes raised here: invalid_config_hash, ci_workspace_not_fresh (begin); ci_platform_unsupported, invalid_name,
# ci_environment_invalid, config_drift, ci_no_executions, ci_mixed_commits, ci_argv_unrepresentable, ci_root_not_top,
# ci_launcher_untracked, ci_config_untracked, ci_workflow_exists, ci_patch_exists, invalid_workspace (generate); plus
# whatever config.load_config and ledger.append raise.

RUNNER_FIELDS = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_WORKFLOW_REF")
PLATFORMS = ("github-actions",)
CI_PIN = "ci-pin"
DEFAULT_NAME = "qa-reexecution"
_RUNNER_VALUE = re.compile(r"[A-Za-z0-9._/@:-]{1,200}")
_CONFIG_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_EXPRESSION = "${{"
_LINE_SEPARATORS = ("\u2028", "\u2029")  # YAML readers that take Unicode line breaks end a line here too
_TEMPLATE = "ci-github-actions.yml.tmpl"
_POLICY = "ci-actions.json"
_ACTIONS = ("checkout", "upload-artifact")
_INITIALIZED = "workspace.initialized"
_CONFIG_FILE = ".qa/config.json"
_STATIC = "static-analysis"
_RUN_DEFAULTS = ("test-execution-local", "functional")  # qa run's own --activity and --category defaults
_TRACKED_MODES = ("100644", "100755")
_BUNDLE = '"$RUNNER_TEMP/qa-evidence-bundle.tar.gz"'


class CiStep(NamedTuple):
    """One recorded run as the job re-runs it. The fields after ``timeout_s`` say how the run was asked for, so the re-run
    pairs with it (``bundle``: the same argv, activity and scope at the same commit); their defaults are ``qa run``'s."""
    execution_id: str
    argv: Tuple[str, ...]
    scope: str
    reports: Tuple[str, ...]  # FORMAT:PATTERN, as `qa run --report` takes them
    timeout_s: Optional[int]  # None: the configuration's default
    activity: str = _RUN_DEFAULTS[0]
    category: str = _RUN_DEFAULTS[1]
    shell: bool = False
    cwd: str = "."            # relative to the workspace root
    allow_empty: bool = False


# --------------------------------------------------------------------------- qa ci begin

def runner_fields(environ: Mapping[str, str]) -> Dict[str, str]:
    """The ``RUNNER_FIELDS`` values that are well formed; nothing else the runner sets is read."""
    return {name: environ[name] for name in RUNNER_FIELDS
            if isinstance(environ.get(name), str) and _RUNNER_VALUE.fullmatch(environ[name])}


def _fresh(ws: Workspace) -> bool:
    """Whether every ledger line, readable or not, is a ``workspace.initialized`` event, and nothing else is in the ledger."""
    files, foreign = ledger._session_files(ws)
    if foreign:
        return False
    return all(envelope is not None and envelope["type"] == _INITIALIZED
               for _session, path in files for _number, envelope in ledger._read_lines(path))


def begin(root: Path, actor: ledger.Actor, *, config_hash: str, environ: Mapping[str, str]) -> dict:
    """Pin the workspace at ``root`` (created if needed, keeping a tracked config.json) to ``config_hash``.

    ``matched`` false records nothing accepted, so the drift rule blocks every later run but static analysis. A runner
    value the ledger's redaction would change is left out, since stored redacted it would no longer fit ``ci.begun``.
    """
    if not isinstance(config_hash, str) or not _CONFIG_HASH.fullmatch(config_hash):
        raise QaError("invalid_config_hash", "the config hash is sha256: and 64 lower-case hex digits")
    ws = workspace.init_workspace(Path(root))[0]
    with ws.lock():
        if not _fresh(ws):
            raise QaError("ci_workspace_not_fresh", "qa ci begin pins a fresh checkout: this ledger already holds events "
                                                    "other than workspace.initialized")
        config.load_config(ws)  # a file the kernel cannot use is refused before anything is pinned to it
        found = config.current_hash(ws)
        matched = found == config_hash
        redactor = redact.load_redactor(ws)
        runner = {name: value for name, value in runner_fields(environ).items() if redactor.text(value)[1] == 0}
        begun = ledger.append(ws, actor, "ci.begun", {"expected": config_hash, "found": found, "matched": matched,
                                                      "runner": runner})
        accepted = None
        if matched:  # the hash that was compared, not the file read again: an edit in between is drift
            accepted = ledger.append(ws, actor, "config.accepted",
                                     {"hash": config_hash, "approval": "ci-pin " + config_hash, "basis": CI_PIN})
    return {"workspace": str(ws.root), "expected": config_hash, "found": found, "matched": matched, "runner": runner,
            "event": begun["id"], "accepted": None if accepted is None else accepted["id"]}


# --------------------------------------------------------------------------- qa ci generate

def load_actions() -> Dict[str, dict]:
    """``shared/policies/ci-actions.json`` by action id, checked against its schema."""
    path = resource_dir("policies") / _POLICY
    policy = loads(path.read_bytes())
    schema.validate(policy, "ci-actions.schema.json")
    actions = {action["id"]: action for action in policy["actions"]}
    if sorted(actions) != sorted(_ACTIONS):
        raise QaError("policy_invalid", "ci-actions.json must pin checkout and upload-artifact once each")
    return actions


def _covered(cfg: dict, environment: str) -> set:
    """The activities the ci environment's standing authorizations list; refused unless it is such an environment."""
    env = next((e for e in cfg["environments"] if e["id"] == environment), None)
    covered = {a for s in cfg["standing_authorizations"] if s["environment"] == environment for a in s["activities"]}
    if env is None or env["class"] != "ci" or not covered:
        raise QaError("ci_environment_invalid", "the environment must be a declared ci environment with a standing "
                                                "authorization in config.json", environment=environment)
    return covered


def _skip_reason(st: dict, covered: set, exe: str) -> Optional[str]:
    record = st["executions"].get(exe)
    if record is None:
        return "unknown_execution"
    if record["isolation"] == "container":  # D22's route needs an image pull on the runner: milestone 5
        return "container_run"
    git = record["git"]
    if git is None or git["dirty"] or not _COMMIT.fullmatch(git["commit"]):
        return "dirty_tree"
    needed = [record["activity"]] + [entry["activity"] for entry in record["authorizations"]]
    if any(activity not in vocab.RUNNER_CONFINED for activity in needed):
        return "activity_not_confined"
    if any(activity not in covered for activity in needed if activity != _STATIC):
        return "not_covered"
    if record["env_values"]:  # CI or NODE_ENV the run had; the job sets none
        return "environment_variables"
    return None


def _recorded_request(ws: Workspace, exe: str, record: dict, default_timeout: int) -> Tuple[Optional[int], bool]:
    """(timeout_s, allow_empty) from the run's own command.json, while its evidence verifies; else the defaults."""
    manifest = record["evidence_manifest"]
    directory = ws.path("evidence", exe)
    if manifest is None or integrity._check_manifest(directory, exe, manifest):
        return None, False
    try:
        document = loads(integrity.read_regular(directory / "command.json"))
    except (OSError, QaError):
        return None, False
    if not isinstance(document, dict):
        return None, False
    timeout = document.get("timeout_s")
    usable = isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout < math.inf
    timeout_s = int(math.ceil(timeout)) if usable and timeout != default_timeout else None
    return timeout_s, document.get("allow_empty") is True


def _step(ws: Workspace, exe: str, record: dict, default_timeout: int) -> CiStep:
    command = record["command"]
    cwd, tree = command["cwd"], record["git"].get("worktree")
    if tree is not None:  # the checkout is the worktree's commit, at the top of the work tree
        cwd = posixpath.relpath(cwd, tree)
    timeout_s, allow_empty = _recorded_request(ws, exe, record, default_timeout)
    return CiStep(exe, tuple(command["argv"]), record["scope"],
                  tuple("%s:%s" % (r["format"], r["pattern"]) for r in record["reports"]), timeout_s,
                  activity=record["activity"], category=record["category"], shell=command["shell"],
                  cwd=posixpath.normpath(cwd), allow_empty=allow_empty)


def _unrepresentable(text: str) -> bool:
    return _EXPRESSION in text or any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in _LINE_SEPARATORS for c in text)


def _refuse_unrepresentable(texts: Sequence[str], **where: str) -> None:
    """Never echoes the text: it is the argument that could not be carried, and may be a secret or the expression itself."""
    if any(_unrepresentable(text) for text in texts):
        raise QaError("ci_argv_unrepresentable", "a workflow cannot carry `${{`, a line break or another control "
                                                 "character as written; record the run without it", **where)


def running_launcher() -> Optional[Path]:
    """The ``qa.py`` beside the running kernel: ``<skill>/scripts/qa.py`` installed, ``shared/launcher/qa.py`` in the source."""
    kernel = Path(__file__).resolve().parent.parent
    if kernel.name == "_kernel" and kernel.parent.name == "scripts":
        return kernel.parent / "qa.py"
    if kernel.name == "kernel" and kernel.parent.name == "shared":
        return kernel.parent / "launcher" / "qa.py"
    return None


def _launcher_path(ws: Workspace, launcher: Optional[str]) -> Optional[str]:
    """``launcher`` (default: the running one) relative to the workspace root, posix; None when it is not under the root."""
    root = os.path.realpath(str(ws.root))
    found = running_launcher() if launcher is None else Path(root, launcher)  # an absolute launcher stays absolute
    if found is None:
        return None
    try:
        where = os.path.realpath(str(found))
        if os.path.commonpath([root, where]) != root or where == root:
            return None
    except ValueError:
        return None
    return Path(os.path.relpath(where, root)).as_posix()


def _git(ws: Workspace) -> "gitinfo._Git":
    try:
        session = gitinfo.session(ws.root)
        top = None if session is None else session.run(["rev-parse", "--show-prefix"])
    except gitinfo.Unavailable:
        session = top = None
    if session is None or top is None or top.returncode != 0:
        raise QaError("ci_launcher_untracked", "git cannot be run on this workspace, so nothing shows the launcher is tracked")
    if top.stdout.strip():
        raise QaError("ci_root_not_top", "the workspace root is not the top of its git work tree, where the job's checkout "
                                         "puts the project")
    return session


def _blob(session: "gitinfo._Git", commit: str, path: str) -> Optional[str]:
    """The object ID of the regular file at ``path`` in ``commit``; None when there is none."""
    try:
        done = session.run(["--literal-pathspecs", "ls-tree", "--full-tree", "-z", commit, "--", path])
    except gitinfo.Unavailable:
        return None
    if done.returncode != 0:
        return None
    for item in done.stdout.split(b"\0"):
        meta, tab, name = item.partition(b"\t")
        fields = meta.decode("ascii", "replace").split(" ")
        if tab and os.fsdecode(name) == path and len(fields) == 3 and fields[0] in _TRACKED_MODES and fields[1] == "blob":
            return fields[2]
    return None


def _config_at(session: "gitinfo._Git", commit: str) -> Optional[str]:
    """The sha256 of ``.qa/config.json`` as ``commit`` holds it."""
    blob = _blob(session, commit, _CONFIG_FILE)
    if blob is None:
        return None
    try:
        done = session.run(["cat-file", "blob", blob])
    except gitinfo.Unavailable:
        return None
    return sha256_bytes(done.stdout) if done.returncode == 0 else None


def _patch(path: str, text: str) -> bytes:
    """A unified diff that creates ``path`` holding ``text``, as ``git apply`` reads it."""
    lines = text.splitlines(keepends=True)
    head = ("diff --git a/{0} b/{0}\nnew file mode 100644\n--- /dev/null\n+++ b/{0}\n@@ -0,0 +1,{1} @@\n"
            .format(path, len(lines)))
    return (head + "".join("+" + line for line in lines)).encode("utf-8")


def _write_patch(ws: Workspace, name: str, data: bytes) -> None:
    """Create ``.qa/work/patches/<name>``, through the kernel's exclusive create; never replace or follow anything there."""
    directory = ws.path("work", "patches")
    for folder in (ws.path("work"), directory):
        try:
            os.mkdir(str(folder))
        except FileExistsError:
            pass
        if not stat.S_ISDIR(os.lstat(str(folder)).st_mode):
            raise QaError("invalid_workspace", "%s is not a directory" % folder.relative_to(ws.root).as_posix())
    target = directory / name
    if os.path.lexists(str(target)) or not workspace._create_new(target, data):
        raise QaError("ci_patch_exists", "this workflow's patch is already written; apply it or remove it first",
                      patch=target.relative_to(ws.root).as_posix())


def generate(ws: Workspace, actor: ledger.Actor, *, platform: str, environment: str, executions: Sequence[str],
             launcher: Optional[str] = None, name: str = DEFAULT_NAME) -> dict:
    """Write the re-execution workflow for ``executions`` as a patch and record ``ci.generated``; returns its data."""
    if platform not in PLATFORMS:
        raise QaError("ci_platform_unsupported", "only github-actions is supported", platform=platform)
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise QaError("invalid_name", "a workflow name is 1-40 lower-case letters, digits and hyphens, not starting with a "
                                      "hyphen", name=name)
    actions = load_actions()
    with ws.lock():
        st = state.load(ws)
        accepted = config.accepted_hash(st)
        if config.current_hash(ws) != accepted:
            raise QaError("config_drift", config.DRIFT_REFUSAL)
        cfg = config.load_config(ws)  # read after the check, so the environment is the accepted file's
        covered = _covered(cfg, environment)
        steps: List[CiStep] = []
        skipped: List[Dict[str, str]] = []
        for exe in dict.fromkeys(str(e) for e in executions):
            reason = _skip_reason(st, covered, exe)
            if reason is None:
                steps.append(_step(ws, exe, st["executions"][exe], cfg["defaults"]["timeout_seconds"]))
            else:
                skipped.append({"execution": exe, "reason": reason})
        if not steps:
            raise QaError("ci_no_executions", "none of the executions can be re-run on the runner", skipped=skipped)
        commits = sorted({st["executions"][s.execution_id]["git"]["commit"] for s in steps})
        if len(commits) > 1:
            raise QaError("ci_mixed_commits", "one workflow checks out one commit; generate one per commit", commits=commits)
        for s in steps:
            _refuse_unrepresentable(s.argv, execution=s.execution_id, field="argv")
            _refuse_unrepresentable(s.reports, execution=s.execution_id, field="report")
            _refuse_unrepresentable([s.cwd], execution=s.execution_id, field="cwd")
        if launcher is not None:
            _refuse_unrepresentable([launcher], field="launcher")
        launcher_path = _launcher_path(ws, launcher)
        if launcher_path is not None:
            _refuse_unrepresentable([launcher_path], field="launcher")
        session = _git(ws)
        if launcher_path is None or _blob(session, commits[0], launcher_path) is None:
            raise QaError("ci_launcher_untracked", "the job runs the launcher the project tracks; this one is not a file "
                                                   "under the root at the target commit", commit=commits[0])
        if _config_at(session, commits[0]) != accepted:
            raise QaError("ci_config_untracked", "%s at the target commit is not the accepted configuration; commit it "
                                                 "first" % _CONFIG_FILE, commit=commits[0])
        workflow_path = ".github/workflows/%s.yml" % name
        if os.path.lexists(str(ws.root / workflow_path)):
            raise QaError("ci_workflow_exists", "the project already has this workflow", workflow=workflow_path)
        text = render_github_actions(name=name, target_commit=commits[0], config_hash=accepted, environment=environment,
                                     launcher=launcher_path, steps=steps, actions=actions)
        digest = sha256_bytes(text.encode("utf-8"))
        patch = "qa-ci-%s.patch" % digest[len("sha256:"):][:12]
        _write_patch(ws, patch, _patch(workflow_path, text))
        data = {"patch": ".qa/work/patches/" + patch, "sha256": digest, "workflow_path": workflow_path, "platform": platform,
                "environment": environment, "target_commit": commits[0], "config_hash": accepted, "launcher": launcher_path,
                "executions": [s.execution_id for s in steps], "skipped": skipped,
                "actions": [dict(actions[a]) for a in _ACTIONS]}
        ledger.append(ws, actor, "ci.generated", data)
    return data


# --------------------------------------------------------------------------- rendering

def _program(path: str) -> str:
    """``path`` as a word after ``python3``: quoted, and never read as an interpreter option."""
    return shlex.quote(path if not path.startswith("-") else "./" + path)


def _uses(action: dict) -> str:
    return "%s@%s # %s" % (action["uses"], action["sha"], action["tag"])


def _run_words(step: CiStep, environment: str) -> List[str]:
    words = ["run", "--environment", environment, "--scope", step.scope]
    if step.activity != _RUN_DEFAULTS[0]:
        words += ["--activity", step.activity]
    if step.category != _RUN_DEFAULTS[1]:
        words += ["--category", step.category]
    for report in step.reports:
        words += ["--report", report]
    if step.timeout_s is not None:
        words += ["--timeout", str(step.timeout_s)]
    if step.allow_empty:
        words.append("--allow-empty")
    if step.shell:
        words.append("--shell")
    return words + ["--"] + list(step.argv)


def _render_step(step: CiStep, environment: str, launcher: str) -> str:
    lines = ["      - name: Re-run %s\n" % step.execution_id, "        if: ${{ !cancelled() }}\n"]
    if step.cwd != ".":
        lines.append("        working-directory: %s\n" % _yaml_text(step.cwd))
    command = " ".join(shlex.quote(word) for word in _run_words(step, environment))
    lines += ["        run: |\n", "          python3 %s %s\n" % (_program(posixpath.relpath(launcher, step.cwd)), command)]
    return "".join(lines)


def _yaml_text(text: str) -> str:
    """A double-quoted scalar; the control characters JSON would escape are already refused."""
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def render_github_actions(*, name: str, target_commit: str, config_hash: str, environment: str, launcher: str,
                          steps: Sequence[CiStep], actions: Mapping[str, dict]) -> str:
    """The workflow text: checkout at ``target_commit``, ``qa ci begin``, one step per run, the bundle and its upload."""
    template = string.Template((resource_dir("templates") / _TEMPLATE).read_text(encoding="utf-8"))
    qa = "python3 " + _program(launcher)
    return template.substitute(
        name=name, target_commit=target_commit, checkout=_uses(actions["checkout"]),
        upload_artifact=_uses(actions["upload-artifact"]),
        begin="%s ci begin --config-hash %s" % (qa, shlex.quote(config_hash)),
        steps="".join(_render_step(step, environment, launcher) for step in steps),
        bundle="%s report bundle --output %s" % (qa, _BUNDLE))
