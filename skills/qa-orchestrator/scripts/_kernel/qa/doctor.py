"""``qa doctor``: the kernel, the ledger, the configuration and this machine's tools in one report."""
from __future__ import annotations

import shutil
import subprocess
from typing import List, Optional

from . import authority, config, envinfo, integrity, ledger, state
from .workspace import Workspace

_DOCKER_TIMEOUT_S = 5


def _docker_server_version() -> Optional[str]:
    """A read-only question: it never starts or changes Docker, and any failure reads as None."""
    path = shutil.which("docker")
    if path is None:
        return None
    try:
        done = subprocess.run([path, "info", "--format", "{{.ServerVersion}}"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=_DOCKER_TIMEOUT_S)
        version = done.stdout.decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    return version if done.returncode == 0 and version else None


def ledger_problems(ws: Workspace) -> List[dict]:
    """``integrity.problems`` as objects: the one answer ``qa doctor`` and ``qa ledger verify`` both give."""
    try:
        found = integrity.problems(ws, state.load(ws))
    except Exception as exc:  # a ledger that cannot even be folded is a problem to report, not a crash
        found = [ledger.Problem("invalid", "ledger", "the ledger could not be checked (%s)" % type(exc).__name__)]
    return [problem._asdict() for problem in found]


def _config_drifted(ws: Workspace) -> bool:
    """False only for a usable config.json that is the accepted one."""
    try:
        config.load_config(ws)
        return config.current_hash(ws) != config.accepted_hash(state.load(ws))
    except Exception:
        return True


def _authority(ws: Workspace) -> dict:
    try:
        report = integrity.authority(ws)
        count = authority.count_without_authority(ws, ledger.iter_all_events(ws))
        return dict(report, key=authority.key_status(ws), events_without_authority=count,
                    recovery=authority.RECOVERY if count else None, limit=authority.LIMIT)
    except Exception as exc:  # what is broken is what doctor reports
        return {"refused": ["authority_unknown: the check failed (%s)" % type(exc).__name__], "limitation": None,
                "key": None, "events_without_authority": None, "recovery": None, "limit": authority.LIMIT}


def doctor(ws: Workspace) -> dict:
    """Never raises for a broken project: what is broken is what it reports."""
    return {
        "kernel": integrity.kernel_integrity(),
        "ledger": ledger_problems(ws),
        "config": {"drift": _config_drifted(ws)},
        "authority": _authority(ws),
        "environment": envinfo.environment_fingerprint(refresh=True),
        "docker": {"server_version": _docker_server_version()},
    }
