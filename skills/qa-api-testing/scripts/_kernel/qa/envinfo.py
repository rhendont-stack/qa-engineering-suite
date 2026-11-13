"""Facts about the machine a run happens on: platform, Python, CPU count, memory and the versions of known tools."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, Optional

_TOOLS = ("git", "docker", "node", "java", "gradle")
_VERSION = re.compile(r"\d+(\.\d+)+")
_TIMEOUT_S = 5
_UNKNOWN = "unknown"  # the tool exists but gave no version; None means it is missing

_cached: Optional[dict] = None


def _memory_bytes() -> Optional[int]:
    """Physical memory, or None where the OS cannot say."""
    try:
        pages, page_size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):  # no sysconf on this platform, a failed call, an unknown name
        return None
    return pages * page_size if pages > 0 and page_size > 0 else None


def host_facts() -> dict:
    """What the interpreter knows without starting a process."""
    return {
        "platform": "%s-%s" % (sys.platform, platform.machine().lower()),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "cpu_count": os.cpu_count(),
        "memory_bytes": _memory_bytes(),
    }


def container_facts(daemon_os: str, daemon_arch: str) -> dict:
    """A container run's environment: the daemon's platform, with the machine that launched it kept apart.

    Nothing about the interpreter or tools inside the image is known without running something in it, so they are left unsaid.
    """
    return {"platform": "%s-%s" % (daemon_os, daemon_arch), "python": None, "implementation": None, "cpu_count": None,
            "memory_bytes": None, "tools": {}, "launcher": host_facts()}


def _version_of(path: str, env: Optional[Dict[str, str]] = None) -> str:
    try:
        # Not in the project: qa doctor runs before any trust decision, and a build tool can read project files at startup.
        with tempfile.TemporaryDirectory(prefix="qa-probe-") as away:
            done = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=_TIMEOUT_S, env=env, cwd=away)
    except (OSError, subprocess.SubprocessError):
        return _UNKNOWN
    if done.returncode != 0:
        return _UNKNOWN
    found = _VERSION.search(done.stdout.decode("utf-8", "replace"))
    return found.group(0) if found else _UNKNOWN


def _tool_version(name: str) -> Optional[str]:
    path = shutil.which(name)
    if path is None:
        return None
    if name != "gradle":
        return _version_of(path)
    home = tempfile.mkdtemp(prefix="qa-gradle-")  # gradle --version writes under its home (§16.8: nothing global)
    try:
        return _version_of(path, dict(os.environ, GRADLE_USER_HOME=home))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def environment_fingerprint(*, refresh: bool = False) -> dict:
    """``host_facts`` plus ``tools``. Cached per process, because each probe starts a process."""
    global _cached
    if _cached is None or refresh:
        _cached = dict(host_facts(), tools={name: _tool_version(name) for name in _TOOLS})
    return _cached
