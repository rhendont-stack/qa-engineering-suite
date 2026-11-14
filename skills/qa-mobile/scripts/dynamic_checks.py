"""Writes the table of the dynamic MASTG checks that a reading of the configuration cannot decide: each one not run here, with
its remediation.

Usage: python3 dynamic_checks.py [--blocked-execution EXE]

The checks are the MASTG tests whose published steps install the app and exercise it on a device or emulator, among the tests
of the weaknesses the static rules map to (MASWE-0026, MASWE-0006, MASWE-0018; MASWE-0063 lists static tests only). Each
entry's status is ``not_run`` with reason ``no-device`` (no device, emulator or simulator here, and no ``device-control``
grant), or ``blocked`` with ``--blocked-execution EXE``, the recorded device run that the guard blocked. No option writes any
other status: a result for one of these checks comes only from a report attached with ``qa ingest``, never from this table.

Writes ``.qa/work/mobile/dynamic-checks.json`` through no link (a fresh temporary file by exclusive create, then a rename onto
the name, replacing an earlier table). Prints one JSON summary. Exit 0 when written, 2 on bad arguments or a refused path.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
OUT = ".qa/work/mobile/dynamic-checks.json"
EXECUTION = re.compile(r"EXE-[0-9A-Z]{26}")
REMEDIATION = ("It needs a device or emulator and a device-control grant, which this machine does not have. "
               "Run it on a machine that has them, then attach the report with qa ingest.")
ATTACH = ("qa ingest bundle FILE imports an evidence bundle made on the machine that ran it; qa ingest junit FILE --from EXE "
          "(or sarif) attaches a report file to this check's blocked execution, recorded as attested: what the file says, "
          "not a run here.")
BASE = "https://mas.owasp.org/MASTG/tests/"
# test -> (platform, MASVS category in its URL, title, weakness, MASVS controls of the weakness, static rules it decides, steps)
CHECKS = [
    ("MASTG-TEST-0216", "android", "MASVS-STORAGE", "Sensitive Data Not Excluded From Backup", "MASWE-0006", ["MASVS-STORAGE-2"],
     ["backup-enabled"],
     "Install the app, use it with sensitive data, back its data up and restore it on a reinstalled app, then list the restored files."),
    ("MASTG-TEST-0298", "ios", "MASVS-STORAGE", "Runtime Monitoring of Files Eligible for Backup", "MASWE-0006", ["MASVS-STORAGE-2"],
     [], "Install the app, hook the file APIs, use it with sensitive data, then list the files it opens that are eligible for backup."),
    ("MASTG-TEST-0236", "android, ios", "MASVS-NETWORK", "Cleartext Traffic Observed on the Network", "MASWE-0026", ["MASVS-NETWORK-1"],
     ["cleartext-traffic", "ats-arbitrary-loads", "ats-insecure-exception"],
     "Capture the app's traffic with a network sniffer or an interception proxy while it is used, then look for cleartext."),
    ("MASTG-TEST-0218", "android", "MASVS-NETWORK", "Insecure TLS Protocols in Network Traffic", "MASWE-0026", ["MASVS-NETWORK-1"], [],
     "Install the app, capture its traffic while it is used, then read the TLS protocol versions it negotiates."),
    ("MASTG-TEST-0348", "ios", "MASVS-NETWORK", "Insecure TLS Protocols in Network Traffic", "MASWE-0026", ["MASVS-NETWORK-1"], [],
     "Install the app, capture its traffic while it is used, then read the TLS protocol versions it negotiates."),
    ("MASTG-TEST-0356", "android", "MASVS-PLATFORM", "Runtime Verification of Unauthorized Database Access through Content Providers",
     "MASWE-0018", ["MASVS-PLATFORM-1"], ["exported-without-permission"],
     "Install the app, use it with sensitive data, then query its exported content providers from outside the app."),
]
NOT_LISTED = [{"test": "MASTG-TEST-0238", "url": BASE + "android/MASVS-NETWORK/MASTG-TEST-0238/",
               "why": "a placeholder on mas.owasp.org with no published steps; MASTG-TEST-0236 covers cleartext on the network"}]


def _writer():
    """``mobile_static.write_new`` and ``Refused``, loaded from beside this file."""
    spec = importlib.util.spec_from_file_location("qa_mobile_static", str(HERE / "mobile_static.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table(execution: Optional[str]) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    for test, platform, category, title, weakness, masvs, rules, steps in CHECKS:
        first = platform.split(",")[0].strip()
        checks.append({
            "test": test, "title": title, "platform": platform, "maswe": weakness, "masvs": masvs, "static_rules": rules,
            "url": "%s%s/%s/%s/" % (BASE, first, category, test), "steps": steps,
            "status": "blocked" if execution else "not_run",
            "reason": ("device-control is not granted: the device run %s was blocked" % execution) if execution
            else "no-device: no device, emulator or simulator here, and no device-control grant",
            "execution": execution, "remediation": REMEDIATION, "attach": ATTACH})
    return {"generated_by": "qa-mobile dynamic_checks.py", "checks": checks, "not_listed": NOT_LISTED,
            "note": "A reading of the configuration decides none of these checks. Each one stays not_run or blocked until a report "
                    "of a run on a device or emulator is attached with qa ingest."}


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--blocked-execution", metavar="EXE")
    args = parser.parse_args(argv)
    writer = _writer()
    try:
        if args.blocked_execution is not None and not EXECUTION.fullmatch(args.blocked_execution):
            raise writer.Refused("--blocked-execution must be an execution id (EXE- and 26 characters)")
        document = table(args.blocked_execution)
        data = (json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8")
        written = writer.write_new(OUT, data)
    except (writer.Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, writer.Refused) else "%s: %s" % (type(exc).__name__, exc)},
                         sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "written": written, "checks": len(document["checks"]),
                      "status": document["checks"][0]["status"], "execution": args.blocked_execution,
                      "tests": [c["test"] for c in document["checks"]],
                      "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
