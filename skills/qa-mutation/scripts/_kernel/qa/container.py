"""The container engine: the one place the kernel spawns it, what may reach it, and the pipeline that runs a project in it.

The engine can sit behind ``ssh`` on a host whose default shell re-parses every word, so nothing from the project may
appear on its command line. ``Engine.run`` lets through only arguments that mean the same to every shell on the way;
paths, scripts and archives travel on stdin. The engine's text comes from ``parse_engine``, which knows three shapes
and refuses everything else. Which of them a workspace may use is ``config.container_engine``'s decision.

``run`` copies an archive of the project into a volume, optionally installs dependencies (the only step with a network),
runs the tests with no network, and copies the evidence out through ``safetar``. There is no bind mount and no host
environment in any step, and no fallback to the host. Every object has a name derived from the execution ID; only those
exact names are ever removed, and a removal that fails is reported.

With a ``ServiceSpec`` a fixture service starts first, detached, with network ``none``, and the tests join its network
namespace: the two share one loopback interface and nothing else. The run counts as isolated only when the daemon
confirms both halves (``_effective_network``).
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
from .workspace import FileTooLarge, read_limited

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
_SERVICE_USER = re.compile(r"[0-9]{1,10}(:[0-9]{1,10})?")
WORK_MOUNT, EVIDENCE_MOUNT, CONTROL_MOUNT = "/work", "/qa-evidence", "/qa-ctl"
INPUT_DIR = ".qa-input"  # where the run's inputs land in the archive; a project entry of that name stays out
SERVICE_SCRIPT = "service.sh"
# Every container that runs project code: the tests, and the install step, which differs only in having a network.
_CONFINED = ("--cpus=2", "--memory=2g", "--pids-limit=512", "--init", "--cap-drop=ALL", "--security-opt=no-new-privileges")
# The steps that only move files (copy in, open the volumes, copy out) run as root whatever user the image names, so an
# image that runs its tool as another user still receives the project and gives its evidence back. They run tar or
# chmod from the image on the suite's own archives, with no network.
_HELPER_USER = "0"
_OPEN_MODE = "0777"


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



def is_service_user(text: object) -> bool:
    """Whether ``text`` is ``UID`` or ``UID:GID`` in digits: a name could mean anything in the image."""
    return isinstance(text, str) and _SERVICE_USER.fullmatch(text) is not None


@dataclass(frozen=True)
class ServiceSpec:
    image: str  # name@sha256:<digest>; never pulled
    script: str  # shell text, run as /qa-ctl/service.sh from ``workdir``; it travels in a tar on stdin
    user: Optional[str] = None  # UID or UID:GID


@dataclass(frozen=True)
class ContainerSpec:
    execution_id: str
    image: str  # name@sha256:<digest>; never pulled
    script: str  # shell text run from ``workdir``
    install_script: Optional[str]
    archive: Path
    workdir: str
    timeout_s: float
    max_bytes: int  # per stream of the test step, the service log, and the evidence copied out
    service: Optional[ServiceSpec] = None


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
    # {"image", "image_id", "network_mode", "running_at_end", "exit_code"} as the daemon described the service; None without one
    service: Optional[dict] = None
    service_log: bytes = b""  # what ``logs`` returned, stdout then stderr, within ``max_bytes``
    service_log_dropped: int = 0  # bytes of it past the cap
    service_log_error: Optional[str] = None  # connection_lost or logs_failed: the log could not be read


# --------------------------------------------------------------------------- the project archive

def _is_skipped(name: str) -> bool:
    parts = name.split("/")
    return any(part.casefold() in _SKIPPED_DIRS for part in parts) or parts[0].casefold() == INPUT_DIR


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


def _plain_name(name: object) -> bool:
    return isinstance(name, str) and bool(name) and not name.startswith("/") and all(
        part not in ("", ".", "..") for part in name.split("/"))


def _directory_entry(name: str) -> tarfile.TarInfo:
    info = _anonymous(tarfile.TarInfo(name))
    info.type, info.mode = tarfile.DIRTYPE, 0o755
    return info


def _pack_inputs(tar: tarfile.TarFile, inputs: Sequence[Tuple[str, Path]], max_bytes: int) -> None:
    """Each input at ``.qa-input/<name>``, readable by any user the service runs as. A source is read once, never through a link."""
    made = set()
    for name, source in inputs:
        if not _plain_name(name):
            raise QaError("project_unreadable", "an input name must be a plain relative path", name=str(name))
        try:
            data = read_limited(Path(source), max_bytes)
        except FileTooLarge:
            raise _too_large(max_bytes) from None
        except OSError as exc:
            raise QaError("project_unreadable", "an input could not be read", name=name, error=type(exc).__name__) from None
        parts = (INPUT_DIR + "/" + name).split("/")
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            if directory not in made:
                made.add(directory)
                tar.addfile(_directory_entry(directory))
        info = _anonymous(tarfile.TarInfo("/".join(parts)))
        info.size, info.mode = len(data), 0o644
        if tar.offset + 2 * tarfile.BLOCKSIZE + len(data) > max_bytes:
            raise _too_large(max_bytes)
        tar.addfile(info, io.BytesIO(data))


def project_archive(root: Path, dest: Path, max_bytes: int, inputs: Sequence[Tuple[str, Path]] = ()) -> int:
    """Pack the project's files into ``dest`` and return its size. Links stay links; no owner from this host goes along.

    ``inputs`` are (name under ``.qa/work/``, file) pairs, packed after the project at ``.qa-input/<name>``."""
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
            _pack_inputs(tar, inputs, max_bytes)
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
    service = spec.service
    if service is not None:
        if not is_pinned(service.image):
            raise QaError("container_spec_invalid", "the service image must be pinned as name@sha256:<digest>", field="service.image")
        if service.user is not None and not is_service_user(service.user):
            raise QaError("container_spec_invalid", "the service user must be UID or UID:GID", field="service.user")
        if not isinstance(service.script, str):
            raise QaError("container_spec_invalid", "the service script must be text", field="service.script")


def _wrapped(script: str) -> bytes:
    """``sh -s`` reads its script from stdin, so the commands get their own empty stdin and cannot read what follows them."""
    return b"(\n" + script.encode("utf-8", "surrogateescape") + b"\n) </dev/null\n"


def _control_archive(dest: Path, script: str) -> None:
    """A tar holding only ``service.sh``: the script reaches its volume on stdin, never on a command line."""
    data = script.encode("utf-8", "surrogateescape") + b"\n"
    info = _anonymous(tarfile.TarInfo(SERVICE_SCRIPT))
    info.size, info.mode = len(data), 0o644
    with tarfile.open(str(dest), "w", format=tarfile.GNU_FORMAT) as tar:
        tar.addfile(info, io.BytesIO(data))


def _first(reported: object) -> dict:
    first = reported[0] if isinstance(reported, list) and reported else None
    return first if isinstance(first, dict) else {}


def _section(info: dict, key: str) -> dict:
    value = info.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _service_facts(image: str, info: dict) -> dict:
    """What the record says about the service. Anything the daemon did not say stays None; not seen running is not running."""
    state = _section(info, "State")
    running = state.get("Running") is True
    code = state.get("ExitCode")
    return {"image": image, "image_id": _text(info.get("Image")), "network_mode": _text(_section(info, "HostConfig").get("NetworkMode")),
            "running_at_end": running, "exit_code": code if not running and isinstance(code, int) and not isinstance(code, bool)
            and state.get("Status") in ("exited", "dead") else None}


def _effective_network(test_mode: str, service_info: Optional[dict]) -> Tuple[str, bool]:
    """(the network mode to record, whether the tests were isolated).

    Without a service the tests are isolated only with mode ``none``. With one, only in the namespace of exactly this run's
    service (``container:<the Id the daemon reports for it>``) whose own mode is ``none``: then the pair shares loopback and
    nothing else, and the record says ``none``. Anything else is the test container's mode as reported, not isolated; a
    test container that reports ``none`` here did not join the service it was started for."""
    if service_info is None:
        return test_mode, test_mode == "none"
    ident = _text(service_info.get("Id"))
    service_mode = _section(service_info, "HostConfig").get("NetworkMode")
    if ident is not None and test_mode == "container:" + ident and service_mode == "none":
        return "none", True
    return test_mode, False


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
        self.work, self.evidence, self.control = self.name + "-work", self.name + "-evidence", self.name + "-ctl"
        self.service = self.name + "-svc"
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

    def _require_ok(self, step: str, done: runner.RunOutcome, image: Optional[str] = None) -> None:
        if self._lost(done):
            raise EngineUnavailable("connection_lost", step=step)
        if done.timed_out:
            raise QaError("container_step_failed", "a pipeline step ran out of time", step=step, reason="timeout")
        if done.exit_code != 0:
            complaint = done.stderr.lower()
            if b"no such image" in complaint or b"unable to find image" in complaint:
                raise QaError("container_image_missing", "the image is not on this engine, and a run never pulls",
                              image=image or self.spec.image)
            raise QaError("container_step_failed", "a pipeline step failed", step=step, exit_code=done.exit_code)

    def _container(self, head: List[str], suffix: str, network: str, entrypoint: str, mounts: Sequence[str],
                   command: Sequence[str], confined: bool = False, image: Optional[str] = None,
                   user: Optional[str] = None) -> List[str]:
        """The arguments for one container; records its name, so cleanup knows it from here on."""
        name = self.name + suffix
        self.containers.append(name)
        args = head + ["--pull=never", "--network=" + network, "--entrypoint=" + entrypoint, self.label, "--name=" + name]
        if confined:
            args += list(_CONFINED) + ["--workdir=" + self.spec.workdir]
        if user is not None:
            args.append("--user=" + user)
        return args + ["--volume=" + mount for mount in mounts] + [image or self.spec.image] + list(command)

    def _create_volumes(self) -> None:
        for volume in (self.work, self.evidence) + ((self.control,) if self.spec.service is not None else ()):
            self.volumes.append(volume)
            self._require_ok("volume_create", self._call(["volume", "create", self.label, volume]))

    def _copy_in(self) -> None:
        args = self._container(["run", "--rm", "-i"], "-in", "none", "tar", [self.work + ":" + WORK_MOUNT],
                               ["-x", "--no-same-owner", "-C", WORK_MOUNT], user=_HELPER_USER)
        self._require_ok("copy_in", self._call(args, stdin_path=self.spec.archive, timeout_s=_COPY_TIMEOUT_S))

    def _open_volumes(self) -> None:
        """The tests run as the image's own user, which need not be root, and a new volume belongs to root: let any user
        write at the top of the working copy and of the evidence volume. Nothing else shares either volume."""
        args = self._container(["run", "--rm"], "-prep", "none", "chmod",
                               [self.work + ":" + WORK_MOUNT, self.evidence + ":" + EVIDENCE_MOUNT],
                               [_OPEN_MODE, WORK_MOUNT, EVIDENCE_MOUNT], user=_HELPER_USER)
        self._require_ok("prepare", self._call(args))

    def _copy_control(self, scratch: Path) -> None:
        archive = scratch / "control.tar"
        _control_archive(archive, self.spec.service.script)
        args = self._container(["run", "--rm", "-i"], "-ctl-in", "none", "tar", [self.control + ":" + CONTROL_MOUNT],
                               ["-x", "--no-same-owner", "-C", CONTROL_MOUNT], user=_HELPER_USER)
        self._require_ok("control_in", self._call(args, stdin_path=archive, timeout_s=_COPY_TIMEOUT_S))

    def _start_service(self) -> None:
        """Detached, with no network of its own: the tests join its namespace, so loopback is all either of them has."""
        service = self.spec.service
        args = self._container(["run", "-d"], "-svc", "none", "sh", [self.work + ":" + WORK_MOUNT + ":ro", self.control + ":" + CONTROL_MOUNT + ":ro"],
                               [CONTROL_MOUNT + "/" + SERVICE_SCRIPT], confined=True, image=service.image, user=service.user)
        self._require_ok("service_start", self._call(args), image=service.image)

    def _describe_service(self) -> Tuple[dict, bytes, int, Optional[str]]:
        """The service as the daemon describes it after the tests, its log and the bytes of it dropped, or why there is no
        log. Neither may cost the test result: a service that cannot be described is simply not counted as isolated."""
        done = self._call(["inspect", self.service])
        info = {} if done.timed_out or done.exit_code != 0 else self._parsed(done.stdout)
        logs = self._call(["logs", self.service], timeout_s=_COPY_TIMEOUT_S, max_bytes=self.spec.max_bytes)
        if self._lost(logs):
            return info, b"", 0, "connection_lost"
        if logs.timed_out or logs.exit_code != 0:
            return info, b"", 0, "logs_failed"
        text = logs.stdout + logs.stderr
        dropped = logs.stdout_dropped + logs.stderr_dropped + max(len(text) - self.spec.max_bytes, 0)
        return info, text[:self.spec.max_bytes], dropped, None

    @staticmethod
    def _parsed(stdout: bytes) -> dict:
        try:
            return _first(loads(stdout))
        except QaError:
            return {}

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
        return self._parsed(done.stdout)

    def _test(self, script: Path) -> Tuple[runner.RunOutcome, dict]:
        network = "none" if self.spec.service is None else "container:" + self.service
        args = self._container(["create", "-i"], "", network, "sh", [self.work + ":" + WORK_MOUNT, self.evidence + ":" + EVIDENCE_MOUNT],
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
                               ["-c", "-C", EVIDENCE_MOUNT, "."], user=_HELPER_USER)
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
        self._open_volumes()
        if self.spec.service is not None:
            self._copy_control(scratch)
        install_ran = self.spec.install_script is not None
        if self.spec.install_script is not None:
            install_path = scratch / "install.sh"
            install_path.write_bytes(_wrapped(self.spec.install_script))
            installed = self._install(install_path)
            if installed.timed_out or installed.exit_code != 0:
                return ContainerOutcome(run=installed, evidence={}, evidence_error="install_failed", install_ran=True, install_failed=True,
                                        network_mode="", image_id="", facts=facts)
        if self.spec.service is not None:
            self._start_service()
        script = scratch / "test.sh"
        script.write_bytes(_wrapped(self.spec.script))
        done, info = self._test(script)
        mode = _section(info, "HostConfig").get("NetworkMode")
        reported = mode if isinstance(mode, str) and mode else "unknown"
        service, service_info, service_log, service_log_dropped, service_log_error = None, None, b"", 0, None
        if self.spec.service is not None:
            service_info, service_log, service_log_dropped, service_log_error = self._describe_service()
            service = _service_facts(self.spec.service.image, service_info)
        network_mode, isolated = _effective_network(reported, service_info)
        image = info.get("Image")
        evidence, evidence_error = self._copy_out()
        return ContainerOutcome(run=done, evidence=evidence, evidence_error=evidence_error, install_ran=install_ran, install_failed=False,
                                network_mode=network_mode, image_id=image if isinstance(image, str) else "", facts=facts,
                                anomalies=() if isolated else ("network_not_isolated",), service=service, service_log=service_log,
                                service_log_dropped=service_log_dropped, service_log_error=service_log_error)

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
