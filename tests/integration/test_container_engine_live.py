"""The engine against a real daemon. Skipped unless QA_TEST_CONTAINER_ENGINE names one.

Every object made here has an exact name from this run and the suite label, and only those names are ever removed.
"""
from __future__ import annotations

import io
import json
import os
import tarfile

import pytest

from qa import container, ids

pytestmark = pytest.mark.docker

IMAGE = "python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b"
LABEL = "--label=qa.suite.test=engine"
TIMEOUT_S = 120
MAX_BYTES = 4 << 20


class Live:
    def __init__(self, engine):
        self.engine = engine
        self.name = "qa-" + ids.new_id("EXE").lower() + "-engine-test"

    def run(self, *args, stdin_path=None):
        return self.engine.run(list(args), stdin_path=stdin_path, timeout_s=TIMEOUT_S, max_bytes=MAX_BYTES)

    def ok(self, *args, stdin_path=None):
        done = self.run(*args, stdin_path=stdin_path)
        assert (done.exit_code, done.stderr) == (0, b"")
        return done


@pytest.fixture
def live_engine():
    text = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not text:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    live = Live(container.Engine(text))
    yield live
    live.run("rm", "-f", live.name)
    live.run("volume", "rm", "-f", live.name + "-work")
    assert live.run("inspect", "--type=container", live.name).exit_code != 0  # by exact name; nothing here ever lists
    assert live.run("volume", "inspect", live.name + "-work").exit_code != 0


def test_the_daemon_reports_its_facts(live_engine):
    facts = live_engine.engine.facts()
    assert facts.version and facts.os == "linux" and facts.arch


def test_stream_round_trip_is_byte_exact(live_engine, tmp_path):
    payload = os.urandom(2 << 20) + b"\r\n\n\r\0\0\r\n" + bytes(range(256))
    stream = tmp_path / "stream"
    stream.write_bytes(payload)
    done = live_engine.ok("run", "--rm", "-i", "--pull=never", "--network=none", "--entrypoint=/bin/cat",
                          LABEL, "--name=" + live_engine.name, IMAGE, stdin_path=stream)
    assert done.stdout == payload and done.stdout_dropped == 0


def _members(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return archive.getnames()


def test_every_flag_the_pipeline_uses_survives_the_engine(live_engine, tmp_path):
    name, volume = live_engine.name, live_engine.name + "-work"
    live_engine.ok("volume", "create", LABEL, volume)
    mount = "--volume=%s:/work" % volume

    archive = tmp_path / "archive.tar"
    with tarfile.open(str(archive), "w") as tar:
        info = tarfile.TarInfo("copied-in.txt")
        info.size, info.uid = 3, 4242
        tar.addfile(info, io.BytesIO(b"in\n"))
    copy_in = name + "-in"
    try:
        live_engine.ok("run", "--rm", "-i", "--pull=never", "--network=none", "--entrypoint=tar", LABEL, "--name=" + copy_in,
                       mount, IMAGE, "-x", "--no-same-owner", "-C", "/work", stdin_path=archive)
    finally:
        live_engine.run("rm", "-f", copy_in)

    live_engine.ok("create", "-i", "--pull=never", "--network=none", "--cpus=2", "--memory=2g", "--pids-limit=512", "--init",
                   "--cap-drop=ALL", "--security-opt=no-new-privileges", "--entrypoint=sh", LABEL,
                   "--label=qa.execution=" + name, "--name=" + name, mount, IMAGE, "-s")
    script = tmp_path / "script.sh"
    script.write_bytes(b"cat /work/copied-in.txt\necho out > /work/written.txt\nstat -c %u /work/copied-in.txt\n")
    done = live_engine.ok("start", "-a", "-i", name, stdin_path=script)
    assert done.stdout == b"in\n0\n"

    config = json.loads(live_engine.ok("inspect", name).stdout)[0]
    host = config["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert (host["NanoCpus"], host["Memory"], host["PidsLimit"], host["Init"]) == (2000000000, 2 << 30, 512, True)
    assert (host["CapDrop"], host["SecurityOpt"]) == (["ALL"], ["no-new-privileges"])
    assert config["Config"]["Labels"]["qa.execution"] == name and config["Config"]["Entrypoint"] == ["sh"]

    copy_out = name + "-out"
    try:
        done = live_engine.ok("run", "--rm", "--pull=never", "--network=none", "--entrypoint=tar", LABEL, "--name=" + copy_out,
                              mount, IMAGE, "-c", "-C", "/work", ".")
    finally:
        live_engine.run("rm", "-f", copy_out)
    assert {"./copied-in.txt", "./written.txt"} <= set(_members(done.stdout))
