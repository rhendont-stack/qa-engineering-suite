"""Runs the tests next to this file with ``unittest`` and writes a JUnit XML report: ``python3 tests/run_junit.py --out report.xml``.

Standard library only. The tests are found relative to this file, so the working directory does not matter. Exits 1 when a test
fails or errors, or when none ran.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class Recorder(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.cases = []
        self._started = 0.0

    def startTest(self, test):
        super().startTest(test)
        self._started = time.time()

    def _record(self, test, outcome, detail):
        kind = type(test)
        self.cases.append((kind.__module__ + "." + kind.__qualname__, test._testMethodName, time.time() - self._started, outcome, detail))

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed", "")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failure", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)


def report(cases):
    suite = ET.Element("testsuite", name="unittest", tests=str(len(cases)),
                       failures=str(sum(1 for case in cases if case[3] == "failure")),
                       errors=str(sum(1 for case in cases if case[3] == "error")),
                       skipped=str(sum(1 for case in cases if case[3] == "skipped")))
    for classname, name, seconds, outcome, detail in cases:
        element = ET.SubElement(suite, "testcase", classname=classname, name=name, time="%.3f" % seconds)
        if outcome != "passed":
            ET.SubElement(element, outcome, message=detail.strip().splitlines()[-1] if detail.strip() else outcome).text = detail
    return ET.ElementTree(suite)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the tests and write a JUnit XML report.")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    sys.path.insert(0, ROOT)
    loaded = unittest.TestLoader().discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    result = Recorder()
    loaded.run(result)
    report(result.cases).write(args.out, encoding="utf-8", xml_declaration=True)
    return 0 if result.cases and result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
