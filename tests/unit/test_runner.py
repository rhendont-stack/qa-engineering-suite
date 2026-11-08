from __future__ import annotations

import os
import signal
import time

import pytest

from qa import runner

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


def run(cmd, tmp_path, **kw):
    return runner.run_process(cmd, shell=False, cwd=tmp_path, env=dict(os.environ),
                              timeout_s=kw.pop("timeout_s", 30),
                              max_bytes=kw.pop("max_bytes", 10**6), **kw)


def test_exit_code_and_output(py, tmp_path):
    r = run(py.argv("import sys; print('hi'); sys.exit(3)"), tmp_path)
    assert (r.exit_code, r.stdout, r.timed_out, r.spawn_error) == (3, b"hi\n", False, None)


def test_cwd_with_space_and_non_ascii(py, tmp_path):
    d = tmp_path / "Programación" / "My Skills"
    d.mkdir(parents=True)
    assert run(py.argv("import os; print(os.getcwd())"), d).stdout.decode().strip() == str(d)


def test_binary_output_byte_faithful(py, tmp_path):
    r = run(py.argv("import sys; sys.stdout.buffer.write(b'\\xff\\x00\\xfe')"), tmp_path)
    assert r.stdout == b"\xff\x00\xfe"


def test_output_cap_counts_dropped(py, tmp_path):
    r = run(py.argv("print('x' * 10000)"), tmp_path, max_bytes=100)
    assert len(r.stdout) == 100 and r.stdout_dropped == 9901


def test_stdin_is_devnull(py, tmp_path):
    assert run(py.argv("import sys; print(repr(sys.stdin.read()))"), tmp_path).stdout == b"''\n"


def test_spawn_not_found(tmp_path):
    r = run(["qa-definitely-not-a-command"], tmp_path)
    assert r.spawn_error == "not_found" and r.exit_code is None


@posix_only
def test_spawn_not_executable(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("x", encoding="utf-8")
    r = run([str(f)], tmp_path)
    assert r.spawn_error == "not_executable" and r.exit_code is None


@posix_only
def test_signal_recorded(py, tmp_path):
    r = run(py.argv("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"), tmp_path)
    assert r.signal == 9 and r.exit_code is None


GRANDCHILD = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "sys.stderr.write(str(p.pid) + '\\n'); sys.stderr.flush()")


def gone(pid, wait=3.0):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@posix_only
def test_timeout_kills_process_group(py, tmp_path):
    r = run(py.argv(GRANDCHILD + "; import time; time.sleep(60)"), tmp_path, timeout_s=1)
    assert r.timed_out and r.duration_ms < 9000 and gone(int(r.stderr.split()[0]))


@posix_only
def test_background_child_holding_stdout(py, tmp_path):
    r = run(py.argv(GRANDCHILD), tmp_path, grace_s=0.5)
    assert r.exit_code == 0 and r.orphaned_output and r.duration_ms < 5000 and gone(int(r.stderr.split()[0]))


@posix_only
def test_returns_when_a_process_outside_the_group_holds_the_pipe(py, tmp_path, request):
    code = ("import subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True); "
            "sys.stderr.write(str(p.pid) + '\\n'); sys.stderr.flush()")
    pids = []

    def cleanup():
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    request.addfinalizer(cleanup)
    r = run(py.argv(code), tmp_path, grace_s=0.5)
    pids.append(int(r.stderr.split()[0]))
    assert r.exit_code == 0 and r.orphaned_output and r.duration_ms < 5000
