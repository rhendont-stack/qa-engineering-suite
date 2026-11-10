from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pytest

from helpers import FIXTURES, KERNEL, LAUNCHER, REPO, fake_engine  # noqa: F401 (a fixture, registered by being imported here)


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


@pytest.fixture
def repo_root():
    return REPO


class FixtureProject:
    """A private copy of one fixture, with a workspace the kernel can run in, driven through the launcher."""

    def __init__(self, root, qa_cli):
        self.root = root
        self._qa_cli = qa_cli

    def cli(self, *args, cwd=None, env=None):
        return self._qa_cli(*args, cwd=cwd or self.root, env=env, workspace=self.root)

    def results(self, execution):
        """{test id: status} the kernel recorded for ``execution``."""
        state = self.cli("state", "show", "results")[1]["state"]
        return {result["test_id"]: result["status"] for result in state.values() if result["execution"] == execution}


@pytest.fixture
def qa_on_fixture(tmp_path, qa_cli):
    """``qa_on_fixture(name, git=False) -> FixtureProject``: a copy of ``fixtures/<name>`` in its own trusted workspace.

    ``git=True`` also makes the copy a work tree with one commit, which the gate needs; without git the test is skipped.
    """
    made = []

    def make(name, git=False):
        root = tmp_path / ("project-%d" % len(made))
        shutil.copytree(str(FIXTURES / name), str(root), ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", "node_modules", ".gradle", "build", ".qa"))
        if git:
            if shutil.which("git") is None:
                pytest.skip("tool missing: git")
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "c"],
                           check=True)
        project = FixtureProject(root, qa_cli)
        assert project.cli("init")[0] == 0
        assert project.cli("config", "set", "project.trust", "trusted", "--approval", "fixture test")[0] == 0
        made.append(project)
        return project

    return make


@pytest.fixture
def qa_on_generated(tmp_path, qa_cli):
    """``qa_on_generated(name, checkout=None) -> FixtureProject``: what ``fixtures/<name>/generate.py`` makes, history included,
    in its own trusted workspace. ``checkout`` stops the history at that commit index. Without git the test is skipped.
    """
    made = []

    def make(name, checkout=None):
        if shutil.which("git") is None:
            pytest.skip("tool missing: git")
        root = tmp_path / ("generated-%d" % len(made))
        extra = [] if checkout is None else ["--checkout", str(checkout)]
        done = subprocess.run([sys.executable, str(FIXTURES / name / "generate.py"), str(root)] + extra, capture_output=True, text=True,
                              timeout=120)
        assert done.returncode == 0, done.stderr
        project = FixtureProject(root, qa_cli)
        assert project.cli("init")[0] == 0
        assert project.cli("config", "set", "project.trust", "trusted", "--approval", "fixture test")[0] == 0
        made.append(project)
        return project

    return make
