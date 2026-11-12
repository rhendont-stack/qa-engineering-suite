"""The container engine: the one place the kernel spawns it, what may reach it, and the pipeline that runs a project in it.

The engine can sit behind ``ssh`` on a host whose default shell re-parses every word, so nothing from the project may
appear on its command line. ``Engine.run`` lets through only arguments that mean the same to every shell on the way;
paths, scripts and archives travel on stdin. The engine's text comes from ``parse_engine``, which knows three shapes
and refuses everything else. Which of them a workspace may use is ``config.container_engine``'s decision.

``run`` copies an archive of the project into a volume, optionally installs dependencies (the only step with a network),
runs the tests with no network, and copies the evidence out through ``safetar``. There is no bind mount and no host
environment in any step, and no fallback to the host. Every object has a name derived from the execution ID; only those
exact names are ever removed, and a removal that fails is reported.
"""
from __future__ import annotations

import io
import os
import re
import stat
import tarfile
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import gitinfo, runner, safetar
from .canonical import loads
from .errors import QaError

# QaError codes raised here: container_engine_invalid, container_unsafe_token, container_stdin_unreadable,
# container_engine_unavailable, container_spec_invalid, container_step_failed, container_image_missing,
# container_cleanup_failed, project_too_large, project_unlistable, project_unreadable.

DEFAULT_ENGINE = "docker"
DEFAULT_ARCHIVE_MAX_BYTES = 512 << 20

# LogLevel=ERROR keeps login banners and host-key notices, which can name the host, out of the engine output that reaches logs.
_SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes", "-o", "LogLevel=ERROR")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_TOKEN = re.compile(r"[A-Za-z0-9./-][A-Za-z0-9@:._/=-]*")
# What the engine client and ssh need to find their daemon, keys and agent. The rest of the environment stays out, so a
# later ``-e NAME`` could not forward a host secret either.
_ENGINE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK",
               "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")
_NEUTRAL_CWD = Path(os.path.abspath(os.sep))  # the engine never runs where a project could plant its configuration
_FACTS_TIMEOUT_S = 30.0
_FACTS_MAX_BYTES = 1 << 20
_CONTROL_TIMEOUT_S = 60.0
_COPY_TIMEOUT_S = 900.0
_CONTROL_MAX_BYTES = 1 << 20
_SKIPPED_DIRS = (".git", ".qa")
_EXECUTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_PINNED_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}")
_WORKDIR = re.compile(r"/work(/[A-Za-z0-9._-]+)*")
WORK_MOUNT, EVIDENCE_MOUNT = "/work", "/qa-evidence"
# Every container that runs project code: the tests, and the install step, which differs only in having a network.
_CONFINED = ("--cpus=2", "--memory=2g", "--pids-limit=512", "--init", "--cap-drop=ALL", "--security-opt=no-new-privileges")


class EngineUnavailable(QaError):
    """The engine could not be started or did not answer. ``details['reason']`` says which way."""

    def __init__(self, reason: str, **details: object) -> None:
        super().__init__("container_engine_unavailable", "the container engine did not answer: %s" % reason,
                         reason=reason, **details)


@dataclass(frozen=True)
class DaemonFacts:
    version: str
    os: str
    arch: str


def parse_engine(text: str) -> Tuple[str, ...]:
    """The argv prefix for ``docker``, ``ssh <alias> docker`` or ``ssh <alias> wsl -e docker``; nothing else is an engine."""
    words = text.split(" ") if isinstance(text, str) else []
    if words == ["docker"]:
        return ("docker",)
    if len(words) >= 3 and words[0] == "ssh" and _ALIAS.fullmatch(words[1]) and words[2:] in (["docker"], ["wsl", "-e", "docker"]):
        return ("ssh",) + _SSH_OPTIONS + tuple(words[1:])
    raise QaError("container_engine_invalid", "the engine must be docker, ssh <alias> docker or ssh <alias> wsl -e docker")


def is_pinned(image: object) -> bool:
    """Whether ``image`` is ``name@sha256:<digest>``, the only form a run accepts: a tag can change under a recorded run."""
    return isinstance(image, str) and _PINNED_IMAGE.fullmatch(image) is not None


def _checked(args: Sequence[str]) -> list:
    """A list copied from ``args``, every item safe for each shell between here and the daemon. What was checked is what runs."""
    if isinstance(args, (str, bytes)):
        raise QaError("container_unsafe_token", "arguments must be a sequence, not one string")
    copied = list(args)
    for position, arg in enumerate(copied):
        if not isinstance(arg, str) or _TOKEN.fullmatch(arg) is None:
            raise QaError("container_unsafe_token", "an engine argument holds characters a shell could act on", position=position)
    return copied


def _engine_env() -> Dict[str, str]:
    return {name: os.environ[name] for name in _ENGINE_ENV if name in os.environ}


class Engine:
    def __init__(self, text: str = DEFAULT_ENGINE) -> None:
        self.argv = parse_engine(text)

    @property
    def kind(self) -> str:
        """What the record may say about the engine: ``local`` or ``ssh``, never the alias."""
        return "ssh" if self.argv[0] == "ssh" else "local"

    @property
    def host(self) -> Optional[str]:
        """The ssh alias, so that its text can be kept out of what is stored; None for a local engine."""
        return self.argv[1 + len(_SSH_OPTIONS)] if self.kind == "ssh" else None

    def run(self, args: Sequence[str], *, stdin_path: Optional[Path], timeout_s: float, max_bytes: int) -> runner.RunOutcome:
        """Run the engine with ``args``. A timeout or a non-zero exit is an outcome: a test in the container can exit 255 too."""
        command = list(self.argv) + _checked(args)
        outcome = runner.run_process(command, shell=False, cwd=_NEUTRAL_CWD, env=_engine_env(), timeout_s=timeout_s,
                                     max_bytes=max_bytes, stdin_path=stdin_path)
        if outcome.spawn_error == "stdin_unreadable":
            raise QaError("container_stdin_unreadable", "the stream for the engine is not a readable regular file")
        if outcome.spawn_error is not None:
            raise EngineUnavailable(outcome.spawn_error)
        return outcome

    def facts(self) -> DaemonFacts:
        """The daemon's version, OS and architecture. Any answer short of those three means there is no usable daemon."""
        outcome = self.run(["version", "--format", "json"], stdin_path=None, timeout_s=_FACTS_TIMEOUT_S, max_bytes=_FACTS_MAX_BYTES)
        if outcome.timed_out:
            raise EngineUnavailable("timeout")
        if outcome.exit_code != 0:
            raise EngineUnavailable("exit_nonzero", exit_code=outcome.exit_code, signal=outcome.signal)
        try:
            reported = loads(outcome.stdout)
        except QaError:
            raise EngineUnavailable("malformed") from None
        server = reported.get("Server") if isinstance(reported, dict) else None
        values = [server.get(key) for key in ("Version", "Os", "Arch")] if isinstance(server, dict) else []
        if len(values) != 3 or not all(isinstance(value, str) and value for value in values):
            raise EngineUnavailable("malformed")
        return DaemonFacts(version=values[0], os=values[1], arch=values[2])



@dataclass(frozen=True)
class ContainerSpec:
    execution_id: str
    image: str  # name@sha256:<digest>; never pulled
    script: str  # shell text run from ``workdir``
    install_script: Optional[str]
    archive: Path
    workdir: str
    timeout_s: float
    max_bytes: int  # per stream of the test step, and the evidence copied out


@dataclass(frozen=True)
class ContainerOutcome:
    run: runner.RunOutcome  # the tests, or the install step when it failed
    evidence: Dict[str, bytes]
    evidence_error: Optional[str]
    install_ran: bool
    install_failed: bool
    network_mode: str
    image_id: str
    facts: DaemonFacts
    anomalies: Tuple[str, ...] = ()
    cleanup_failed: Tuple[str, ...] = ()  # exact names of objects that could not be removed


# --------------------------------------------------------------------------- the project archive

def _is_skipped(name: str) -> bool:
    return any(part.casefold() in _SKIPPED_DIRS for part in name.split("/"))


def _raise(exc: OSError) -> None:
    raise exc


def _walked(root: Path) -> List[str]:
    found: List[str] = []
    for here, dirs, files in os.walk(str(root), onerror=_raise):
        dirs[:] = [d for d in dirs if d.casefold() not in _SKIPPED_DIRS]
        where = os.path.relpath(here, str(root))
        links = [d for d in dirs if os.path.islink(os.path.join(here, d))]  # a link to a directory stays a link
        found += [leaf if where == "." else "%s/%s" % (where, leaf) for leaf in files + links]
    return found


def _project_names(root: Path) -> List[str]:
    listed = gitinfo.project_files(root)
    if listed is None:
        if os.path.lexists(str(root / ".git")):  # a walk here would pack exactly what the project's .gitignore keeps out
            raise QaError("project_unlistable", "git could not list this repository's files")
        try:
            listed = _walked(root)
        except OSError as exc:
            raise QaError("project_unreadable", "a project directory could not be read", error=type(exc).__name__) from None
    return sorted({name.rstrip("/") for name in listed if not _is_skipped(name.rstrip("/"))})


def _stays_inside(root: Path, name: str) -> bool:
    """Whether ``name`` is a plain relative path with a real directory at every parent.

    ``git ls-files -c`` reports the index without looking at the working tree, and ``lstat`` and ``tar.add`` guard only the
    last component, so a tracked path whose parent became a link would pack whatever the link points at.
    """
    parts = name.split("/")
    if name.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return False
    here = str(root)
    for part in parts[:-1]:
        here = os.path.join(here, part)
        if not stat.S_ISDIR(os.lstat(here).st_mode):
            return False
    return True


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _too_large(limit: int) -> QaError:
    return QaError("project_too_large", "the project archive would exceed the cap", limit=limit)


def project_archive(root: Path, dest: Path, max_bytes: int) -> int:
    """Pack the project's files into ``dest`` and return its size. Links stay links; no owner from this host goes along."""
    names = _project_names(root)
    try:
        with tarfile.open(str(dest), "w", format=tarfile.GNU_FORMAT) as tar:
            for name in names:
                path = os.path.join(str(root), name)
                try:
                    if not _stays_inside(root, name):
                        continue
                    status = os.lstat(path)
                    if not (stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode) or stat.S_ISDIR(status.st_mode)):
                        continue
                    if tar.offset + tarfile.BLOCKSIZE + (status.st_size if stat.S_ISREG(status.st_mode) else 0) > max_bytes:
                        raise _too_large(max_bytes)
                    tar.add(path, arcname=name, recursive=False, filter=_anonymous)
                except FileNotFoundError:  # tracked but deleted in the working tree
                    continue
                except OSError as exc:
                    raise QaError("project_unreadable", "a project file could not be read", name=name, error=type(exc).__name__) from None
        size = os.stat(str(dest)).st_size
        if size > max_bytes:
            raise _too_large(max_bytes)
    except BaseException:
        try:
            os.unlink(str(dest))
        except FileNotFoundError:
            pass
        raise
    return size


# --------------------------------------------------------------------------- the pipeline

def _require_valid(spec: ContainerSpec) -> None:
    """Every field that becomes an engine argument, before the first spawn: an image or ID could otherwise read as a flag."""
    if _EXECUTION_ID.fullmatch(spec.execution_id) is None:
        raise QaError("container_spec_invalid", "the execution ID cannot name a container", field="execution_id")
    if not is_pinned(spec.image):
        raise QaError("container_spec_invalid", "the image must be pinned as name@sha256:<digest>", field="image")
    if _WORKDIR.fullmatch(spec.workdir) is None or any(part in (".", "..") for part in spec.workdir.split("/")):
        raise QaError("container_spec_invalid", "the working directory must lie under /work", field="workdir")


def _wrapped(script: str) -> bytes:
    """``sh -s`` reads its script from stdin, so the commands get their own empty stdin and cannot read what follows them."""
    return b"(\n" + script.encode("utf-8", "surrogateescape") + b"\n) </dev/null\n"


def _read_evidence(stdout: bytes, max_bytes: int) -> Tuple[Dict[str, bytes], Optional[str]]:
    """All the regular files of the copied-out tar, or nothing at all: part of a hostile archive is not evidence."""
    try:
        return dict(safetar.read_members(io.BytesIO(stdout), max_bytes, from_tree=True)), None
    except QaError as exc:
        return {}, exc.code


class _Pipeline:
    def __init__(self, engine: Engine, spec: ContainerSpec) -> None:
        self.engine, self.spec = engine, spec
        self.name = "qa-" + spec.execution_id.lower()
        self.work, self.evidence = self.name + "-work", self.name + "-evidence"
        self.label = "--label=qa.execution=" + spec.execution_id
        self.containers: List[str] = []  # each name is recorded before the spawn that could create it
        self.volumes: List[str] = []

    def _call(self, args: List[str], stdin_path: Optional[Path] = None, timeout_s: float = _CONTROL_TIMEOUT_S,
              max_bytes: int = _CONTROL_MAX_BYTES) -> runner.RunOutcome:
        return self.engine.run(args, stdin_path=stdin_path, timeout_s=timeout_s, max_bytes=max_bytes)

    def _lost(self, done: runner.RunOutcome) -> bool:
        """``ssh`` exits 255 when the connection fails, and so can a command. Only a fresh probe tells them apart."""
        if done.exit_code != 255:
            return False
        try:
            self.engine.facts()
        except EngineUnavailable:
            return True
        return False

    def _require_ok(self, step: str, done: runner.RunOutcome) -> None:
        if self._lost(done):
            raise EngineUnavailable("connection_lost", step=step)
        if done.timed_out:
            raise QaError("container_step_failed", "a pipeline step ran out of time", step=step, reason="timeout")
        if done.exit_code != 0:
            complaint = done.stderr.lower()
            if b"no such image" in complaint or b"unable to find image" in complaint:
                raise QaError("container_image_missing", "the image is not on this engine, and a run never pulls", image=self.spec.image)
            raise QaError("container_step_failed", "a pipeline step failed", step=step, exit_code=done.exit_code)

    def _container(self, head: List[str], suffix: str, network: str, entrypoint: str, mounts: Sequence[str],
                   command: Sequence[str], confined: bool = False) -> List[str]:
        """The arguments for one container; records its name, so cleanup knows it from here on."""
        name = self.name + suffix
        self.containers.append(name)
        args = head + ["--pull=never", "--network=" + network, "--entrypoint=" + entrypoint, self.label, "--name=" + name]
        if confined:
            args += list(_CONFINED) + ["--workdir=" + self.spec.workdir]
        return args + ["--volume=" + mount for mount in mounts] + [self.spec.image] + list(command)

    def _create_volumes(self) -> None:
        for volume in (self.work, self.evidence):
            self.volumes.append(volume)
            self._require_ok("volume_create", self._call(["volume", "create", self.label, volume]))

    def _copy_in(self) -> None:
        args = self._container(["run", "--rm", "-i"], "-in", "none", "tar", [self.work + ":" + WORK_MOUNT],
                               ["-x", "--no-same-owner", "-C", WORK_MOUNT])
        self._require_ok("copy_in", self._call(args, stdin_path=self.spec.archive, timeout_s=_COPY_TIMEOUT_S))

    def _install(self, script: Path) -> runner.RunOutcome:
        args = self._container(["run", "--rm", "-i"], "-install", "bridge", "sh", [self.work + ":" + WORK_MOUNT], ["-s"], confined=True)
        done = self._call(args, stdin_path=script, timeout_s=self.spec.timeout_s, max_bytes=self.spec.max_bytes)
        if self._lost(done):
            raise EngineUnavailable("connection_lost", step="install")
        return done

    def _inspect(self) -> dict:
        """What the daemon says about the test container; empty when it will not say."""
        done = self._call(["inspect", self.name])
        if self._lost(done):
            raise EngineUnavailable("connection_lost", step="inspect")
        if done.timed_out or done.exit_code != 0:
            return {}
        try:
            reported = loads(done.stdout)
        except QaError:
            return {}
        first = reported[0] if isinstance(reported, list) and reported else None
        return first if isinstance(first, dict) else {}

    def _test(self, script: Path) -> Tuple[runner.RunOutcome, dict]:
        args = self._container(["create", "-i"], "", "none", "sh", [self.work + ":" + WORK_MOUNT, self.evidence + ":" + EVIDENCE_MOUNT],
                               ["-s"], confined=True)
        self._require_ok("create", self._call(args))
        done = self._call(["start", "-a", "-i", self.name], stdin_path=script, timeout_s=self.spec.timeout_s, max_bytes=self.spec.max_bytes)
        if done.timed_out:
            self._call(["kill", self.name])  # over ssh the container outlives its client; cleanup's rm -f has the last word
            return done, self._inspect()
        if self._lost(done):
            raise EngineUnavailable("connection_lost", step="test")
        info = self._inspect()
        state = info.get("State") if isinstance(info.get("State"), dict) else {}
        status, code = state.get("Status"), state.get("ExitCode")
        if status == "created":
            raise QaError("container_step_failed", "the test container never started", step="start", exit_code=done.exit_code)
        # Only a container the daemon reports as exited has a result. Anything else means the client ended apart from it: a
        # running container still shows ExitCode 0, and a 255 the container did not produce came from the link.
        if status != "exited" or not isinstance(code, int) or (done.exit_code == 255 and code != 255):
            raise EngineUnavailable("connection_lost", step="test")
        return replace(done, exit_code=code), info  # a shell on the way can fold every failure into 1; the daemon cannot

    def _copy_out(self) -> Tuple[Dict[str, bytes], Optional[str]]:
        args = self._container(["run", "--rm"], "-out", "none", "tar", [self.evidence + ":" + EVIDENCE_MOUNT + ":ro"],
                               ["-c", "-C", EVIDENCE_MOUNT, "."])
        done = self._call(args, timeout_s=_COPY_TIMEOUT_S, max_bytes=self.spec.max_bytes)
        if self._lost(done):
            return {}, "connection_lost"
        if done.timed_out or done.exit_code != 0 or done.orphaned_output:
            return {}, "copy_out_failed"
        if done.stdout_dropped:
            return {}, "evidence_too_large"
        return _read_evidence(done.stdout, self.spec.max_bytes)

    def steps(self, scratch: Path, facts: DaemonFacts) -> ContainerOutcome:
        self._create_volumes()
        self._copy_in()
        install_ran = self.spec.install_script is not None
        if self.spec.install_script is not None:
            install_path = scratch / "install.sh"
            install_path.write_bytes(_wrapped(self.spec.install_script))
            installed = self._install(install_path)
            if installed.timed_out or installed.exit_code != 0:
                return ContainerOutcome(run=installed, evidence={}, evidence_error="install_failed", install_ran=True, install_failed=True,
                                        network_mode="", image_id="", facts=facts)
        script = scratch / "test.sh"
        script.write_bytes(_wrapped(self.spec.script))
        done, info = self._test(script)
        host = info.get("HostConfig")
        mode = host.get("NetworkMode") if isinstance(host, dict) else None
        network_mode = mode if isinstance(mode, str) and mode else "unknown"
        image = info.get("Image")
        evidence, evidence_error = self._copy_out()
        return ContainerOutcome(run=done, evidence=evidence, evidence_error=evidence_error, install_ran=install_ran, install_failed=False,
                                network_mode=network_mode, image_id=image if isinstance(image, str) else "", facts=facts,
                                anomalies=("network_not_isolated",) if network_mode != "none" else ())

    def _removed(self, args: List[str]) -> bool:
        try:
            done = self._call(args)
        except QaError:  # an engine that cannot even be started has removed nothing
            return False
        if done.timed_out:
            return False
        if done.exit_code == 0:
            return True
        complaint = done.stderr.lower()  # only the daemon's own answer about this object; a transport error can say "no such" too
        return done.exit_code not in (None, 255) and (b"no such container" in complaint or b"no such volume" in complaint)

    def remove_own_objects(self) -> Tuple[str, ...]:
        """Remove this run's containers and then its volumes, each by exact name. The names that could not be removed come back."""
        failed = [name for name in self.containers if not self._removed(["rm", "-f", name])]
        failed += [name for name in self.volumes if not self._removed(["volume", "rm", "-f", name])]
        return tuple(failed)


def run(engine: Engine, spec: ContainerSpec) -> ContainerOutcome:
    """Run ``spec`` through the pipeline. A failure of the pipeline itself raises a typed ``QaError``, never a test outcome."""
    _require_valid(spec)
    facts = engine.facts()
    pipeline = _Pipeline(engine, spec)
    cleanup_failed: Tuple[str, ...] = ()
    try:
        try:
            with tempfile.TemporaryDirectory(prefix="qa-container-") as scratch:
                outcome = pipeline.steps(Path(scratch), facts)
        finally:
            cleanup_failed = pipeline.remove_own_objects()
    except BaseException as exc:
        if cleanup_failed:
            if not isinstance(exc, QaError):
                raise QaError("container_cleanup_failed", "objects were left behind", cleanup_failed=list(cleanup_failed)) from exc
            exc.details["cleanup_failed"] = list(cleanup_failed)
        raise
    return replace(outcome, cleanup_failed=cleanup_failed)
