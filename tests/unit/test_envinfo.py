from __future__ import annotations

import os
import platform
import subprocess
import sys

import pytest

from qa import envinfo

TOOLS = {"git", "docker", "node", "java", "gradle"}


def test_fingerprint_is_cached_until_refresh():
    a = envinfo.environment_fingerprint()
    assert envinfo.environment_fingerprint() is a and envinfo.environment_fingerprint(refresh=True) is not a
    assert a["python"] == platform.python_version() and set(a["tools"]) == {"git", "docker", "node", "java", "gradle"}


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """A PATH holding only stub tools; the fixture's teardown restores the process cache."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(envinfo, "_cached", None)

    def add(name, body):
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    return add


def tools():
    return envinfo.environment_fingerprint(refresh=True)["tools"]


def test_missing_tool_is_null(stubs):
    assert tools() == dict.fromkeys(TOOLS)


def test_first_dotted_number_of_the_version_output(stubs):
    stubs("git", 'echo "git version 2.52.0 (Apple Git-155)"')
    stubs("node", "echo v25.5.0")
    stubs("docker", 'echo "Docker version 29.9.2, build a82e528 1.2"')
    found = tools()
    assert (found["git"], found["node"], found["docker"]) == ("2.52.0", "25.5.0", "29.9.2")


def test_version_output_on_stderr_counts(stubs):
    stubs("java", 'echo "openjdk 17.0.18 2026-01-20" 1>&2')
    assert tools()["java"] == "17.0.18"


def test_found_tool_without_a_dotted_number_is_unknown(stubs):
    stubs("git", "echo no version here")
    stubs("node", "echo v25")   # one number is not a dotted number
    found = tools()
    assert (found["git"], found["node"]) == ("unknown", "unknown")


def test_nonzero_exit_is_unknown(stubs):
    stubs("docker", 'echo "Docker version 1.2.3"; exit 3')
    assert tools()["docker"] == "unknown"


def test_timeout_is_unknown(stubs, monkeypatch):
    monkeypatch.setattr(envinfo, "_TIMEOUT_S", 0.2)
    stubs("node", 'exec /bin/sleep 30')
    assert tools()["node"] == "unknown"


def test_spawn_failure_is_unknown(stubs, tmp_path):
    (tmp_path / "bin" / "git").write_text("no interpreter line\n")   # exec fails with ENOEXEC
    (tmp_path / "bin" / "git").chmod(0o755)
    assert tools()["git"] == "unknown"


def test_gradle_gets_a_private_home_that_is_gone_afterwards(stubs, tmp_path):
    seen = tmp_path / "gradle-home.txt"
    stubs("gradle", 'echo "$GRADLE_USER_HOME" > "%s"; test -d "$GRADLE_USER_HOME" && echo "Gradle 9.5.0"' % seen)
    assert tools()["gradle"] == "9.5.0"
    home = seen.read_text().strip()
    assert home and not os.path.exists(home)


def test_probes_have_stdin_closed_and_a_timeout(stubs, monkeypatch):
    stubs("git", "echo 2.52.0")
    calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        calls.append(kw)
        return real_run(cmd, **kw)

    monkeypatch.setattr(envinfo.subprocess, "run", spy)
    tools()
    assert calls and all(c["stdin"] == subprocess.DEVNULL and c["timeout"] == 5 for c in calls)


def test_a_found_tool_is_probed_with_version_flag(stubs):
    stubs("git", '[ "$1" = "--version" ] && echo 2.52.0')
    assert tools()["git"] == "2.52.0"


def test_host_facts_start_no_process(monkeypatch):
    def refuse(*_args, **_kw):
        raise AssertionError("host_facts must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    facts = envinfo.host_facts()
    assert set(facts) == {"platform", "python", "implementation", "cpu_count", "memory_bytes"}
    assert facts["python"] == platform.python_version() and facts["implementation"] == platform.python_implementation()


def test_platform_is_sys_platform_and_lower_machine():
    assert envinfo.host_facts()["platform"] == "%s-%s" % (sys.platform, platform.machine().lower())


def test_fingerprint_has_the_host_facts_and_tools():
    fingerprint = envinfo.environment_fingerprint()
    assert set(fingerprint) == {"platform", "python", "implementation", "cpu_count", "memory_bytes", "tools"}
    assert {k: fingerprint[k] for k in envinfo.host_facts()} == envinfo.host_facts()


# --------------------------------------------------------------------------- CPU count and memory (T14-R22)

def _sysconf(monkeypatch, **answers):
    """``os.sysconf`` answers from ``answers``, raising the ones that are exceptions."""
    def fake(name):
        if isinstance(answers[name], Exception):
            raise answers[name]
        return answers[name]

    monkeypatch.setattr(os, "sysconf", fake)


def test_cpu_count_is_what_the_os_says(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert envinfo.host_facts()["cpu_count"] == 8


def test_cpu_count_can_be_unknown(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert envinfo.host_facts()["cpu_count"] is None


def test_memory_is_physical_pages_times_page_size(monkeypatch):
    _sysconf(monkeypatch, SC_PHYS_PAGES=1000, SC_PAGE_SIZE=4096)
    assert envinfo.host_facts()["memory_bytes"] == 4096000


@pytest.mark.parametrize("pages, size", [
    (ValueError("unknown name"), 4096), (1000, ValueError("unknown name")),
    (OSError(22, "invalid"), 4096), (1000, OSError(22, "invalid")),
    (0, 4096), (-1, 4096), (1000, 0), (1000, -1), (-1, -1),
])
def test_memory_is_null_when_the_os_cannot_say(monkeypatch, pages, size):
    _sysconf(monkeypatch, SC_PHYS_PAGES=pages, SC_PAGE_SIZE=size)
    assert envinfo.host_facts()["memory_bytes"] is None


def test_memory_is_null_without_sysconf(monkeypatch):
    monkeypatch.delattr(os, "sysconf")
    assert envinfo.host_facts()["memory_bytes"] is None


def test_the_real_host_answers_with_a_positive_integer_or_null():
    facts = envinfo.host_facts()
    for value in (facts["cpu_count"], facts["memory_bytes"]):
        assert value is None or (type(value) is int and value > 0)
