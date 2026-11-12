"""The numbers of one recorded k6 run, each with its label: percentiles, error rate, throughput.

Usage: python3 perf_table.py --execution EXE-<id>

Run from the project root. The execution is read through the skill's own kernel (``qa.py state show executions``), never
from a file someone could have written. Only a run whose status is ``passed`` or ``failed`` measured anything; for any other
status the table says so (``"measured": false`` and why) and holds no number.

The numbers come from the k6 report the run stored (the execution's ``report.metrics``, which the kernel read from the
summary) or, for a run with none, from a k6 summary attached to it later with ``qa ingest k6 FILE --from EXE`` whose
``strength`` the kernel recorded as ``structured``: a file the run's own evidence manifest lists, so the run produced it
(read under ``.qa/evidence/<EXE>/``, never through a link, at most 16 MiB). A summary the kernel recorded as ``attested`` (a
file the run did not produce, attached after it) is never read as the run's numbers: it is listed under ``attested`` and
gives no row, no label and no threshold verdict. Each row is labelled:

* ``measured``: read from the report of a run in an environment of class ``staging`` or ``production``;
* ``simulated``: read from the report of a run in a class below ``staging`` (``local``, ``ci``, ``ephemeral``,
  ``shared-test``): a measurement of this copy on this machine, standing in for the real deployment, never a figure of it;
* ``derived``: computed here from the rows above it, with the formula given.

Rows: ``http_req_duration`` avg, med, p(90), p(95), p(99), max (ms); ``http_req_failed`` rate (the error rate);
``http_reqs`` count and rate (the throughput, requests a second); ``iterations`` count; ``vus_max`` max; and, derived, the
successful requests a second (``http_reqs.rate * (1 - http_req_failed.rate)``) and the error rate as a percentage. A
statistic the report does not hold is left out, never guessed. The threshold verdicts (each objective, ``ok`` or crossed)
come from the stored summary when it can be read.

Writes ``.qa/work/performance/perf-<EXE>.json`` (never through a link) and prints the same JSON. Exit 0 with a table or with
``"measured": false``; 2 on a bad argument, an unknown execution, or a kernel answer that cannot be read. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "qa.py"
EXE = re.compile(r"EXE-[0-9A-Z]{26}")
BELOW_STAGING = ("local", "ci", "ephemeral", "shared-test")
AT_LEAST_STAGING = ("staging", "production")
MAX_SUMMARY = 16 << 20
ROWS = (("http_req_duration", ("avg", "med", "p(90)", "p(95)", "p(99)", "max"), "ms"),
        ("http_req_failed", ("rate",), "fraction of requests"),
        ("http_reqs", ("count",), "requests"),
        ("http_reqs", ("rate",), "requests/s"),
        ("iterations", ("count",), "iterations"),
        ("vus_max", ("max",), "VUs"))


class Refused(Exception):
    pass


def finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


# --------------------------------------------------------------------------- reading

def executions(root: Path) -> Dict[str, Any]:
    done = subprocess.run([sys.executable, "-I", "-B", str(LAUNCHER), "--role", "verifier", "--skill", "qa-performance", "state", "show",
                           "executions"], cwd=str(root), capture_output=True, timeout=120)
    try:
        answer = json.loads(done.stdout.decode("utf-8"))
        found = answer["state"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise Refused("the kernel's state show executions gave no readable answer (exit %d)" % done.returncode) from None
    if not isinstance(found, dict):
        raise Refused("the kernel's state show executions gave no executions")
    return found


def read_evidence(root: Path, execution: str, relative: str) -> Optional[bytes]:
    """A regular file under ``.qa/evidence/<execution>/``, opened without following a link anywhere on the way."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    here = root
    for part in (".qa", "evidence", execution) + pure.parts[:-1]:
        here = here / part
        try:
            mode = os.lstat(str(here)).st_mode
        except OSError:
            return None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return None
    try:
        fd = os.open(str(here / pure.parts[-1]), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        data = handle.read(MAX_SUMMARY + 1)
    return None if len(data) > MAX_SUMMARY else data


def summary_of(data: Optional[bytes]) -> Optional[Dict[str, Any]]:
    if data is None:
        return None
    try:
        doc = json.loads(data.decode("utf-8"), parse_constant=lambda _name: None)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("metrics"), dict) else None


def k6_files(record: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """``(the k6 summaries backed by the run, latest last; the attested ones)``. Backed: the reports the run stored, and the
    summaries ingested into it whose recorded ``strength`` is ``structured`` (its own evidence manifest lists the file).
    Any other ingested summary, ``attested`` or with no strength recorded, is not the run's."""
    files = [str(f) for r in record.get("reports") or [] if isinstance(r, dict) and r.get("format") == "k6" and not r.get("error")
             for f in r.get("files") or [] if isinstance(f, str)]
    attested = []
    for item in record.get("ingested") or []:
        if not (isinstance(item, dict) and item.get("format") == "k6" and isinstance(item.get("file"), str) and item["file"]):
            continue
        (files if item.get("strength") == "structured" else attested).append(item["file"])
    return files, attested


def metrics_of(record: Dict[str, Any], summaries: List[Tuple[str, Dict[str, Any]]]) -> Tuple[Optional[Dict[str, Dict[str, Any]]], str]:
    report = record.get("report")
    if isinstance(report, dict) and report.get("format") == "k6" and isinstance(report.get("metrics"), dict) and report["metrics"]:
        return report["metrics"], "the run's k6 report (execution record)"
    for name, doc in reversed(summaries):
        values = {metric: raw.get("values") for metric, raw in doc["metrics"].items() if isinstance(raw, dict) and isinstance(raw.get("values"), dict)}
        if values:
            return values, "the k6 summary %s" % name
    return None, ""


def threshold_verdicts(summaries: List[Tuple[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not summaries:
        return []
    found = []
    for metric, raw in sorted(summaries[-1][1]["metrics"].items()):
        verdicts = raw.get("thresholds") if isinstance(raw, dict) else None
        for expression, verdict in sorted(verdicts.items()) if isinstance(verdicts, dict) else ():
            ok = verdict.get("ok") if isinstance(verdict, dict) else None
            if isinstance(ok, bool):
                found.append({"metric": str(metric)[:200], "threshold": str(expression)[:200], "ok": ok})
    return found


# --------------------------------------------------------------------------- the table

def label_for(record: Dict[str, Any]) -> Tuple[Optional[str], str]:
    environment = record.get("environment")
    klass = environment.get("class") if isinstance(environment, dict) else None
    if klass in BELOW_STAGING:
        return "simulated", ("measured in a %s environment: it stands in for the real deployment and is not a figure of it" % klass)
    if klass in AT_LEAST_STAGING:
        return "measured", "measured in a %s environment" % klass
    return None, "the run's environment class is not recorded"


def rows_of(metrics: Dict[str, Dict[str, Any]], label: str, execution: str) -> List[Dict[str, Any]]:
    rows = []
    for metric, stats, unit in ROWS:
        values = metrics.get(metric)
        for stat_name in stats:
            value = finite(values.get(stat_name)) if isinstance(values, dict) else None
            if value is not None:
                rows.append({"metric": metric, "stat": stat_name, "value": round(value, 3), "unit": unit, "label": label,
                             "execution": execution})
    rate = finite((metrics.get("http_reqs") or {}).get("rate")) if isinstance(metrics.get("http_reqs"), dict) else None
    failed = finite((metrics.get("http_req_failed") or {}).get("rate")) if isinstance(metrics.get("http_req_failed"), dict) else None
    if rate is not None and failed is not None:
        rows.append({"metric": "successful requests", "stat": "rate", "value": round(rate * (1 - failed), 3), "unit": "requests/s",
                     "label": "derived", "from": "http_reqs.rate * (1 - http_req_failed.rate)", "execution": execution})
    if failed is not None:
        rows.append({"metric": "error rate", "stat": "percent", "value": round(failed * 100, 3), "unit": "%", "label": "derived",
                     "from": "http_req_failed.rate * 100", "execution": execution})
    return rows


def table(root: Path, execution: str) -> Dict[str, Any]:
    found = executions(root)
    record = found.get(execution)
    if not isinstance(record, dict):
        raise Refused("%s is not a recorded execution of this workspace" % execution)
    status, reason = record.get("status"), record.get("reason")
    out: Dict[str, Any] = {"ok": True, "execution": execution, "status": status, "reason": reason,
                           "anomalies": record.get("anomalies") or [],
                           "load_limit_exceeded": [n for n in record.get("limitations") or [] if isinstance(n, str) and n.startswith("load_limit_exceeded")]}
    if status not in ("passed", "failed"):
        out.update(measured=False, why="a %s run measured nothing%s" % (status, (": " + str(reason)) if reason else ""), rows=[])
        return out
    backed, attested = k6_files(record)
    out["attested"] = [name[:200] for name in attested]
    summaries = [(name, doc) for name in backed for doc in [summary_of(read_evidence(root, execution, name))] if doc is not None]
    metrics, source = metrics_of(record, summaries)
    label, note = label_for(record)
    if metrics is None or label is None:
        if metrics is None:
            why = "no k6 metrics in the run's record or in a summary the run produced"
            if attested:
                why += ("; the k6 summaries attached to it (%s) are attested, not the run's: the kernel did not find them in its "
                        "evidence manifest" % ", ".join(out["attested"][:5]))
        else:
            why = note
        out.update(measured=False, why=why, rows=[])
        return out
    out.update(measured=True, source=source, environment_class=record["environment"]["class"], label_note=note,
               rows=rows_of(metrics, label, execution), thresholds=threshold_verdicts(summaries))
    return out


def write_table(root: Path, execution: str, data: bytes) -> str:
    parts = [".qa", "work", "performance", "perf-%s.json" % execution]
    here = root
    for position, part in enumerate(parts[:-1]):
        here = here / part
        try:
            mode = os.lstat(str(here)).st_mode
        except FileNotFoundError:
            if position != len(parts) - 2:
                raise Refused("%s does not exist (run the kernel's init first)" % "/".join(parts[:position + 1])) from None
            os.mkdir(str(here))
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("%s is a link or not a directory" % "/".join(parts[:position + 1]))
    temporary = here / (".%s.%s.tmp" % (parts[-1], secrets.token_hex(8)))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(str(temporary), str(here / parts[-1]))
    except BaseException:
        if os.path.lexists(str(temporary)):
            os.unlink(str(temporary))
        raise
    return "/".join(parts)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise Refused("arguments: " + message)


def main(argv: Sequence[str]) -> int:
    parser = _Parser(description=__doc__.splitlines()[0])
    parser.add_argument("--execution", required=True)
    root = Path.cwd()
    try:
        args = parser.parse_args(list(argv))
        if not EXE.fullmatch(args.execution):
            raise Refused("--execution must be an execution id, EXE- and 26 characters")
        result = table(root, args.execution)
        result["written"] = ".qa/work/performance/perf-%s.json" % args.execution
        write_table(root, args.execution, (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    except (Refused, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
