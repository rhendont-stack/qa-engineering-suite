"""Process runner: the one place the kernel starts a project command.

``run_process`` starts the command with ``stdin`` closed, in its own session (so its whole
process group can be signalled), and reads both pipes on one thread each. A reader keeps the
first ``max_bytes`` of its stream and counts the rest as dropped. Output stays byte-faithful;
decoding and redaction belong to the caller.

Time: waits and ``duration_ms`` use ``time.monotonic()``; ``started_at`` and ``ended_at`` go
through ``ids.now_utc`` so tests can freeze them.

Timeout: SIGTERM to the group, up to 5 s for the main process, then SIGKILL to the group in
every case. After the main process exits, ``grace_s`` bounds the wait for both pipes to
close. If a pipe stays open, the group is killed and the readers get ``grace_s`` more. Then
``run_process`` returns what was collected with ``orphaned_output`` set, even if a process
outside the group still holds the pipe (the reader threads are daemons).
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

from qa import ids

TERM_WAIT_S = 5.0


@dataclass(frozen=True)
class RunOutcome:
    exit_code: Optional[int]
    signal: Optional[int]
    timed_out: bool
    spawn_error: Optional[str]
    started_at: str
    ended_at: str
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_dropped: int
    stderr_dropped: int
    orphaned_output: bool


class _Reader(threading.Thread):
    def __init__(self, pipe, max_bytes: int) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._max = max_bytes
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._dropped = 0
        self.closed = threading.Event()

    def run(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(65536)
                if not chunk:
                    break
                with self._lock:
                    room = self._max - len(self._buf)
                    if room > 0:
                        self._buf += chunk[:room]
                    self._dropped += max(len(chunk) - max(room, 0), 0)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._pipe.close()
            except OSError:
                pass
            self.closed.set()

    def snapshot(self):
        with self._lock:
            return bytes(self._buf), self._dropped


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_readers(readers, seconds: float) -> bool:
    end = time.monotonic() + max(seconds, 0.0)
    for r in readers:
        if not r.closed.wait(max(end - time.monotonic(), 0.0)):
            return False
    return True


def run_process(command: Union[List[str], str], *, shell: bool, cwd: Path, env: Dict[str, str],
                timeout_s: float, max_bytes: int, grace_s: float = 5.0) -> RunOutcome:
    started_at = ids.format_ts(ids.now_utc())
    t0 = time.monotonic()

    def outcome(**kw) -> RunOutcome:
        base = dict(exit_code=None, signal=None, timed_out=False, spawn_error=None,
                    started_at=started_at, ended_at=ids.format_ts(ids.now_utc()),
                    duration_ms=int((time.monotonic() - t0) * 1000), stdout=b"", stderr=b"",
                    stdout_dropped=0, stderr_dropped=0, orphaned_output=False)
        base.update(kw)
        return RunOutcome(**base)

    try:
        proc = subprocess.Popen(command, shell=shell, cwd=str(cwd), env=env, bufsize=0,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError:
        return outcome(spawn_error="not_found")
    except PermissionError:
        return outcome(spawn_error="not_executable")
    except (OSError, ValueError):
        return outcome(spawn_error="spawn_failed")

    pgid = proc.pid
    try:
        readers = [_Reader(proc.stdout, max_bytes), _Reader(proc.stderr, max_bytes)]
        for r in readers:
            r.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=TERM_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
            _kill_group(pgid, signal.SIGKILL)
            proc.wait()

        orphaned = False
        if timed_out:
            orphaned = not _wait_readers(readers, grace_s)
        elif not _wait_readers(readers, grace_s):
            _kill_group(pgid, signal.SIGKILL)
            _wait_readers(readers, grace_s)
            orphaned = True

    except BaseException:
        # No RunOutcome reaches the caller, so only this function can stop the detached group.
        _kill_group(pgid, signal.SIGKILL)
        proc.wait()
        raise

    rc = proc.returncode
    (out, out_dropped), (err, err_dropped) = readers[0].snapshot(), readers[1].snapshot()
    return outcome(exit_code=rc if rc >= 0 else None, signal=-rc if rc < 0 else None,
                   timed_out=timed_out, stdout=out, stderr=err, stdout_dropped=out_dropped,
                   stderr_dropped=err_dropped, orphaned_output=orphaned)
