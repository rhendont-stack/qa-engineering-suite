from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pytest

from helpers import KERNEL, LAUNCHER


class FrozenNow:
    """Controls ``qa.ids.now_utc``; time moves only through ``set`` and ``advance``."""

    def __init__(self) -> None:
        real = datetime.now(timezone.utc)
        self._now = real.replace(microsecond=real.microsecond // 1000 * 1000)

    def __call__(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
            raise ValueError("frozen_now.set needs an aware UTC datetime")
        self._now = dt

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@pytest.fixture
def frozen_now(monkeypatch):
    import qa.ids

    clock = FrozenNow()
    monkeypatch.setattr(qa.ids, "now_utc", clock)
    return clock


class Py:
    """Runs the kernel under the test interpreter in a subprocess."""

    def __init__(self) -> None:
        self.env: Dict[str, str] = dict(os.environ)
        self.env["PYTHONPATH"] = str(KERNEL)

    def argv(self, code: str, *args: str) -> List[str]:
        return [sys.executable, "-c", code, *args]

    def run(self, code: str, *args: str, **kw) -> "subprocess.CompletedProcess":
        options = dict(env=self.env, capture_output=True, text=True, timeout=60)
        options.update(kw)
        return subprocess.run(self.argv(code, *args), **options)


@pytest.fixture
def py():
    return Py()


@pytest.fixture
def ws(tmp_path):
    from qa.workspace import init_workspace

    return init_workspace(tmp_path / "Programación" / "My Skills")[0]


@pytest.fixture
def actor():
    from qa.ledger import Actor

    return Actor(role="verifier", skill="qa-test-execution", session="S-" + "0" * 26, independence="I1")


@pytest.fixture
def trusted(ws, actor):
    from qa import config

    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "project.trust", "trusted", "test approval")
    return ws


_WS_FIXTURE = object()  # qa_cli's default workspace: the ws fixture's root, created only when a test asks for it


@pytest.fixture
def qa_cli(request):
    """``qa_cli(*args, cwd=None, env=None, workspace=<ws>) -> (exit code, the one JSON object stdout carried)``."""
    def run(*args, cwd=None, env=None, workspace=_WS_FIXTURE):
        if workspace is _WS_FIXTURE:
            workspace = request.getfixturevalue("ws").root
        target = [] if workspace is None else ["--workspace", workspace]
        done = subprocess.run([sys.executable, LAUNCHER, *target, *args], cwd=cwd, env=dict(os.environ, **(env or {})),
                              capture_output=True, timeout=120)
        return done.returncode, json.loads(done.stdout)
    return run
