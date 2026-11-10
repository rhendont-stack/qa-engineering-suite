"""The whole pipeline against a real daemon. Skipped unless QA_TEST_CONTAINER_ENGINE names one.

Every object has an exact name from this run's execution ID, and nothing here lists or removes anything else.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qa import config, container, execution, ids, state
from qa.canonical import loads

pytestmark = pytest.mark.docker

IMAGE = "python@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b"
LIMIT = 8 << 20
NO_ROUTE = "import socket; s = socket.socket(); s.settimeout(4); s.connect(('1.1.1.1', 53))"


@pytest.fixture
def live(tmp_path):
    text = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not text:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    return Live(container.Engine(text), tmp_path)


class Live:
    def __init__(self, engine, tmp_path):
        self.engine = engine
        self.execution_id = ids.new_id("EXE")
        self.name = "qa-" + self.execution_id.lower()
        self.root = tmp_path / "my project"
        (self.root / "a dir").mkdir(parents=True)
        (self.root / "a dir" / "in put.txt").write_bytes(b"from the host\n")
        self.archive = tmp_path / "project.tar"
        container.project_archive(self.root, self.archive, 1 << 20)

    def run(self, script, install=None, timeout_s=120.0):
        spec = container.ContainerSpec(execution_id=self.execution_id, image=IMAGE, script=script, install_script=install,
                                       archive=self.archive, workdir="/work", timeout_s=timeout_s, max_bytes=LIMIT)
        return container.run(self.engine, spec)

    def assert_nothing_left(self):
        for args in (["inspect", "--type=container", self.name], ["inspect", "--type=container", self.name + "-in"],
                     ["inspect", "--type=container", self.name + "-install"], ["inspect", "--type=container", self.name + "-out"],
                     ["volume", "inspect", self.name + "-work"], ["volume", "inspect", self.name + "-evidence"]):
            done = self.engine.run(args, stdin_path=None, timeout_s=60, max_bytes=1 << 20)
            assert done.exit_code == 1 and b"no such" in done.stderr.lower(), (args, done.exit_code, done.stderr)  # 255 is the ssh link


def test_tests_have_no_network(live):
    done = live.run("python -c \"%s\"; echo $? > /qa-evidence/connect.txt; ls /sys/class/net > /qa-evidence/interfaces.txt" % NO_ROUTE)
    assert (done.network_mode, done.anomalies, done.cleanup_failed, done.evidence_error) == ("none", (), (), None)
    assert done.evidence["connect.txt"] == b"1\n" and done.evidence["interfaces.txt"] == b"lo\n"
    assert done.image_id.startswith("sha256:")
    live.assert_nothing_left()


def test_evidence_comes_back_intact(live):
    payload = os.urandom(1 << 16) + b"\r\n\0\n" + bytes(range(256))
    source = live.root / "payload.bin"
    source.write_bytes(payload)
    container.project_archive(live.root, live.archive, 1 << 20)
    script = ("mkdir -p '/qa-evidence/sub dir'\n"
              "cp payload.bin '/qa-evidence/sub dir/payload.bin'\n"
              "cat 'a dir/in put.txt' > /qa-evidence/read.txt\n"
              "cat > /qa-evidence/stdin.txt\n"
              "echo written >> payload.bin\n"
              "exit 7\n")
    done = live.run(script)
    assert done.run.exit_code == 7 and done.evidence_error is None
    assert done.evidence == {"sub dir/payload.bin": payload, "read.txt": b"from the host\n", "stdin.txt": b""}
    live.assert_nothing_left()


def test_timeout_leaves_no_container_or_volume(live):
    done = live.run("echo started > /qa-evidence/started.txt\nsleep 300\n", timeout_s=6.0)
    assert done.run.timed_out is True and done.cleanup_failed == ()
    assert done.evidence == {"started.txt": b"started\n"}
    live.assert_nothing_left()


def test_install_alone_has_a_network_and_prepares_the_project(live):
    install = "ls /sys/class/net | sort > installed.txt\necho dep >> installed.txt\n"
    done = live.run("cp installed.txt /qa-evidence/installed.txt\nls /sys/class/net > /qa-evidence/interfaces.txt\n", install=install)
    assert (done.install_ran, done.install_failed, done.run.exit_code) == (True, False, 0)
    assert done.evidence["interfaces.txt"] == b"lo\n"
    assert b"eth0" in done.evidence["installed.txt"] and done.evidence["installed.txt"].endswith(b"dep\n")
    live.assert_nothing_left()


def test_an_exit_of_255_from_the_test_is_a_test_result(live):
    done = live.run("exit 255\n")
    assert (done.run.exit_code, done.evidence_error) == (255, None)
    live.assert_nothing_left()


def test_a_link_planted_in_the_evidence_is_report_missing(live):
    done = live.run("echo fine > /qa-evidence/ok.txt\nln -s /etc/passwd /qa-evidence/planted\n")
    assert (done.run.exit_code, done.evidence, done.evidence_error) == (0, {}, "archive_unsafe")
    live.assert_nothing_left()


def test_an_absent_image_is_not_pulled(live):
    spec = container.ContainerSpec(execution_id=live.execution_id, image="python@sha256:" + "0" * 64, script="true", install_script=None,
                                   archive=live.archive, workdir="/work", timeout_s=60.0, max_bytes=LIMIT)
    with pytest.raises(container.QaError) as refused:
        container.run(live.engine, spec)
    assert refused.value.code == "container_image_missing"
    live.assert_nothing_left()


CATALOG = [(entry["id"], entry["image"]) for entry in
           loads((Path(container.__file__).resolve().parents[2] / "policies" / "container-images.json").read_bytes())["images"]]


@pytest.mark.parametrize("name, image", CATALOG, ids=[name for name, _image in CATALOG])
def test_qa_run_container_on_each_catalog_image(ws, actor, name, image):
    text = os.environ.get("QA_TEST_CONTAINER_ENGINE")
    if not text:
        pytest.skip("tool missing: QA_TEST_CONTAINER_ENGINE is not set")
    config.record_initialized(ws, actor)
    config.set_value(ws, actor, "container.engine", text, "the live suite's own engine")
    (ws.root / "in put.txt").write_bytes(b"from the host\n")
    write = "grep -q 'from the host' 'in put.txt' && printf '<testsuite><testcase classname=\"c\" name=\"%s\"/></testsuite>' \"$(uname -m)\" > {evidence}/junit.xml"
    result = execution.execute(ws, actor, execution.RunRequest(command=write, shell=True, container=image, reports=(("junit", "{evidence}/junit.xml"),), timeout_s=120))
    assert (result["status"], result["reason"], result["strength"]) == ("passed", None, "structured"), result
    record = state.load(ws)["executions"][result["execution"]]
    assert record["container"]["network_mode"] == "none"
    assert record["environment"]["platform"] == "linux-amd64"
    assert not any("container_cleanup_failed" in text_ for text_ in record["limitations"])
    host = text.split(" ")[1] if text.startswith("ssh ") else None
    assert host is None or host not in json.dumps(record)
