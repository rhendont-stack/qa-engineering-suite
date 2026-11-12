"""Writes a k6 script from a workload, within the limits a load-test grant carries.

Usage: python3 k6_script.py --workload .qa/work/performance/<name>.json --max-vus N --max-duration-s S [--max-rps R]
                            --out .qa/work/performance/<name>.js

The workload is a JSON object (``references/workload-model.md``):

    {"name": "orders-listing", "model": "closed", "vus": 20, "duration_s": 60, "think_time_s": 1,
     "requests": [{"name": "list orders", "method": "GET", "path": "/orders",
                   "headers": {"Authorization": "Bearer user-a-token"}, "expect": 200, "weight": 1}],
     "thresholds": {"http_req_duration": ["p(95)<200"], "http_req_failed": ["rate<0.01"]}}

``model`` is ``closed`` (``vus`` users, each sending a request then waiting ``think_time_s``, which must be above 0) or
``open`` (``rate_per_s`` requests a second, from up to ``vus`` users; no think time). The script is
``assets/k6-template.js`` with the workload and the k6 options written in: one scenario of ``duration_s`` seconds
(``constant-vus`` or ``constant-arrival-rate``) with a 2-second graceful stop, every threshold as
``{threshold, abortOnFail: true, delayAbortEval: '5s'}``, the trend statistics up to ``p(99)``, and response bodies
discarded. The base URL is never in the script: the run passes ``-e BASE_URL=http://127.0.0.1:<port>`` and the script refuses
any other; ``handleSummary`` writes the whole summary to ``__ENV.SUMMARY_PATH``.

Refused (one JSON line with ``"ok": false``, exit 2, nothing written):

* ``vus`` above ``--max-vus``; ``duration_s`` plus 5 seconds (the stop and k6's start) above ``--max-duration-s``;
* with ``--max-rps``: an open ``rate_per_s`` above it, or a closed workload whose ``vus / think_time_s`` (the most requests a
  second it can send) is above it;
* any URL (``scheme://``) or protocol-relative path anywhere in the workload, a path that does not start with one ``/``, a
  ``Host`` header (or another hop-by-hop or length header), a header or name with a line break or a control character;
* a threshold on an unknown metric or with an expression k6 would not read as one comparison, and a workload with none;
* unknown or duplicate keys, a boolean where a number goes, NaN or Infinity, nesting deeper than six levels, a file over
  64 KiB, a workload or output path outside ``.qa/work/performance/`` or reached through a link, and a missing ``.qa/work/``
  (the kernel's ``init`` makes it).

On success it prints one JSON line: the written path, its sha256, the path the container run sees
(``/work/.qa-input/performance/<name>.js``), the workload's shape and the limits it was checked against. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "assets" / "k6-template.js"
WORK = (".qa", "work", "performance")
MAX_WORKLOAD = 64 * 1024
STOP_S = 2
MARGIN_S = 5            # the graceful stop plus k6's own start and end, kept out of the grant's duration
MAX_REQUESTS = 20
MAX_BODY = 16 * 1024
MAX_DEPTH = 6           # workload > requests > request > headers, and a request body object two levels deep
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _./:()-]{0,79}")
HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
FORBIDDEN_HEADERS = ("host", "connection", "content-length", "transfer-encoding", "te", "upgrade", "proxy-connection", "keep-alive",
                     "trailer", "forwarded", "x-forwarded-host", "x-forwarded-for")
PATH = re.compile(r"/(?!/)[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,511}")
METRICS = ("http_req_duration", "http_req_waiting", "http_req_connecting", "http_req_tls_handshaking", "http_req_sending",
           "http_req_receiving", "http_req_blocked", "http_req_failed", "http_reqs", "iterations", "iteration_duration", "checks",
           "data_received", "data_sent", "vus", "vus_max", "dropped_iterations")
METRIC_KEY = re.compile(r"(?P<metric>[a-z_]+)(?:\{(?:name|method|status|scenario|expected_response):[A-Za-z0-9 _./-]{1,80}\})?")
EXPRESSION = re.compile(r"(?:avg|min|max|med|count|rate|value|p\((?:\d{1,2}(?:\.\d{1,3})?)\))\s*(?:<|<=|>|>=|==|===|!=)\s*\d{1,9}(?:\.\d{1,6})?")
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|(?:^|[\s\"'=(,])//[\w\[]")
WORKLOAD_KEYS = {"name", "model", "vus", "duration_s", "think_time_s", "rate_per_s", "requests", "thresholds", "request_timeout_s"}
REQUEST_KEYS = {"name", "method", "path", "headers", "expect", "weight", "body"}


class Refused(Exception):
    pass


# --------------------------------------------------------------------------- paths, without following links

def work_parts(text: str, suffix: str, what: str) -> List[str]:
    pure = PurePosixPath(text)
    parts = list(pure.parts)
    if (pure.is_absolute() or ".." in parts or "." in parts or tuple(parts[:3]) != WORK or len(parts) != 4
            or not NAME.fullmatch(pure.stem) or pure.suffix != suffix or text != str(pure)):
        raise Refused("%s must be a file directly under .qa/work/performance/, named [a-z0-9-]%s" % (what, suffix))
    return parts


def no_link_on_the_way(root: Path, parts: Sequence[str], create: bool) -> None:
    """Every folder of ``parts`` is a real directory; with ``create``, only the last one may be made (``.qa/work/`` comes
    from ``qa init``)."""
    here = root
    for position, part in enumerate(parts):
        here = here / part
        try:
            mode = os.lstat(str(here)).st_mode
        except FileNotFoundError:
            if not create or position != len(parts) - 1:
                raise Refused("%s does not exist (run the kernel's init first)" % here.relative_to(root).as_posix()) from None
            os.mkdir(str(here))
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise Refused("%s is a link or not a directory" % here.relative_to(root).as_posix())


def read_workload(root: Path, text: str) -> bytes:
    parts = work_parts(text, ".json", "--workload")
    no_link_on_the_way(root, parts[:-1], create=False)
    try:
        fd = os.open(str(root.joinpath(*parts)), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise Refused("--workload is missing or a link") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refused("--workload is not a regular file")
        data = handle.read(MAX_WORKLOAD + 1)
    if len(data) > MAX_WORKLOAD:
        raise Refused("--workload is over %d bytes" % MAX_WORKLOAD)
    return data


def write_script(root: Path, text: str, data: bytes) -> str:
    """A fresh temporary file beside the target, then a rename that replaces the name itself; never through a link."""
    parts = work_parts(text, ".js", "--out")
    no_link_on_the_way(root, parts[:-1], create=True)
    folder = root.joinpath(*parts[:-1])
    temporary = folder / (".%s.%s.tmp" % (parts[-1], secrets.token_hex(8)))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(str(temporary), str(folder / parts[-1]))
    except BaseException:
        if os.path.lexists(str(temporary)):
            os.unlink(str(temporary))
        raise
    return "/".join(parts)


# --------------------------------------------------------------------------- the workload

def _pairs(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise Refused("duplicate key %s" % json.dumps(key)[:80])
        out[key] = value
    return out


def _constant(name: str) -> Any:
    raise Refused("%s is not a JSON number" % name)


def parse(data: bytes) -> Dict[str, Any]:
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused("the workload is not UTF-8 JSON") from None
    if too_deep(doc, MAX_DEPTH):
        raise Refused("the workload nests deeper than the format has (%d levels)" % MAX_DEPTH)
    if not isinstance(doc, dict):
        raise Refused("the workload must be a JSON object")
    return doc


def too_deep(value: Any, limit: int) -> bool:
    """Whether objects and lists nest more than ``limit`` levels; read without recursion."""
    stack = [(value, 0)]
    while stack:
        item, level = stack.pop()
        if isinstance(item, (dict, list)):
            if level + 1 > limit:
                return True
            stack.extend((child, level + 1) for child in (item.values() if isinstance(item, dict) else item))
    return False


def integer(doc: Dict[str, Any], key: str, low: int, high: int) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Refused("%s must be a whole number from %d to %d" % (key, low, high))
    return value


def number(doc: Dict[str, Any], key: str, low: float, high: float) -> float:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low < value <= high:
        raise Refused("%s must be a number above %s and at most %s" % (key, low, high))
    return float(value)


def plain_text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Refused("%s must be a non-empty single-line string of at most %d characters" % (what, limit))
    return value


def request_entry(raw: Any, index: int) -> Dict[str, Any]:
    if not isinstance(raw, dict) or not set(raw) <= REQUEST_KEYS or not {"name", "method", "path", "expect"} <= set(raw):
        raise Refused("requests[%d] needs name, method, path and expect, and only %s" % (index, ", ".join(sorted(REQUEST_KEYS))))
    name = plain_text(raw["name"], "requests[%d].name" % index, 80)
    if not LABEL.fullmatch(name):
        raise Refused("requests[%d].name may hold letters, digits, spaces and . _ / : ( ) -" % index)
    method = raw["method"]
    if method not in METHODS:
        raise Refused("requests[%d].method must be one of %s" % (index, ", ".join(METHODS)))
    path = raw["path"]
    if not isinstance(path, str) or not PATH.fullmatch(path):
        raise Refused("requests[%d].path must be a path on the service, starting with one /, never a URL" % index)
    headers = raw.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 20:
        raise Refused("requests[%d].headers must be an object of at most 20 headers" % index)
    for header, value in headers.items():
        if not HEADER_NAME.fullmatch(header) or header.lower() in FORBIDDEN_HEADERS:
            raise Refused("requests[%d] may not set the header %s" % (index, json.dumps(header)[:70]))
        plain_text(value, "requests[%d].headers.%s" % (index, header), 1024)
    expect = raw["expect"]
    if isinstance(expect, bool) or not isinstance(expect, int) or not 100 <= expect <= 599:
        raise Refused("requests[%d].expect must be an HTTP status from 100 to 599" % index)
    weight = integer(dict(weight=raw.get("weight", 1)), "weight", 1, 100)
    body = raw.get("body")
    if body is not None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=True, sort_keys=True)
        if not isinstance(body, str) or len(body) > MAX_BODY:
            raise Refused("requests[%d].body must be null, a string or a JSON value of at most %d characters" % (index, MAX_BODY))
        if method in ("GET", "HEAD"):
            raise Refused("requests[%d] sends a body with %s" % (index, method))
    return {"name": name, "method": method, "path": path, "headers": dict(headers), "expect": expect, "weight": weight, "body": body}


def thresholds(raw: Any) -> Dict[str, List[Dict[str, Any]]]:
    if not isinstance(raw, dict) or not raw:
        raise Refused("thresholds must name at least one metric: the objectives are the run's tests")
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, expressions in raw.items():
        match = METRIC_KEY.fullmatch(key) if isinstance(key, str) else None
        if match is None or match.group("metric") not in METRICS:
            raise Refused("thresholds: %s is not a k6 metric this script measures" % json.dumps(key)[:80])
        if not isinstance(expressions, list) or not expressions or len(expressions) > 10:
            raise Refused("thresholds.%s must be a list of one to ten expressions" % key)
        items = []
        for expression in expressions:
            if not isinstance(expression, str) or not EXPRESSION.fullmatch(expression):
                raise Refused("thresholds.%s: %s is not one comparison such as p(95)<200 or rate<0.01" % (key, json.dumps(expression)[:60]))
            items.append({"threshold": expression, "abortOnFail": True, "delayAbortEval": "5s"})
        out[key] = items
    return out


def workload(doc: Dict[str, Any], max_vus: int, max_duration_s: int, max_rps: Optional[int]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(the workload the script reads, the k6 options)``, or Refused."""
    unknown = sorted(set(doc) - WORKLOAD_KEYS)
    if unknown:
        raise Refused("unknown workload keys: %s" % ", ".join(json.dumps(k)[:40] for k in unknown[:5]))
    if URL.search(json.dumps(doc, ensure_ascii=False)) or URL.search(json.dumps(doc)):
        raise Refused("the workload holds a URL: the base URL comes from the run, never from the workload")
    name = doc.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused("name must be lower-case letters, digits and hyphens")
    model = doc.get("model")
    if model not in ("closed", "open"):
        raise Refused("model must be closed or open")
    vus = integer(doc, "vus", 1, 100000)
    if vus > max_vus:
        raise Refused("vus %d is above --max-vus %d" % (vus, max_vus))
    duration_s = integer(doc, "duration_s", 1, 86400)
    if duration_s + MARGIN_S > max_duration_s:
        raise Refused("duration_s %d plus %d seconds for the stop is above --max-duration-s %d" % (duration_s, MARGIN_S, max_duration_s))
    timeout_s = integer(dict(request_timeout_s=doc.get("request_timeout_s", 10)), "request_timeout_s", 1, 60)
    if model == "closed":
        if "rate_per_s" in doc:
            raise Refused("a closed workload has no rate_per_s: its users wait think_time_s between requests")
        think = number(doc, "think_time_s", 0, 60)
        bound = vus / think
        if max_rps is not None and bound > max_rps:
            raise Refused("%d users waiting %s s can send %.1f requests a second, above --max-rps %d" % (vus, think, bound, max_rps))
        scenario: Dict[str, Any] = {"executor": "constant-vus", "vus": vus, "duration": "%ds" % duration_s, "gracefulStop": "%ds" % STOP_S}
    else:
        if doc.get("think_time_s", 0) != 0:
            raise Refused("an open workload has no think time: its rate_per_s sets the pace")
        rate = integer(doc, "rate_per_s", 1, 100000)
        if max_rps is not None and rate > max_rps:
            raise Refused("rate_per_s %d is above --max-rps %d" % (rate, max_rps))
        think, bound = 0.0, float(rate)
        scenario = {"executor": "constant-arrival-rate", "rate": rate, "timeUnit": "1s", "duration": "%ds" % duration_s,
                    "preAllocatedVUs": vus, "maxVUs": vus, "gracefulStop": "%ds" % STOP_S}
    raw_requests = doc.get("requests")
    if not isinstance(raw_requests, list) or not 1 <= len(raw_requests) <= MAX_REQUESTS:
        raise Refused("requests must be a list of 1 to %d requests" % MAX_REQUESTS)
    requests = [request_entry(raw, i) for i, raw in enumerate(raw_requests)]
    options = {"scenarios": {"load": scenario}, "thresholds": thresholds(doc.get("thresholds")), "discardResponseBodies": True,
               "summaryTrendStats": ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"]}
    data = {"name": name, "model": model, "think_time_s": think, "request_timeout": "%ds" % timeout_s, "requests": requests}
    shape = {"name": name, "model": model, "vus": vus, "duration_s": duration_s, "think_time_s": think,
             "rate_per_s": doc.get("rate_per_s"), "most_requests_per_s": round(bound, 3),
             "requests": ["%s %s" % (r["method"], r["path"]) for r in requests],
             "thresholds": {k: [i["threshold"] for i in v] for k, v in options["thresholds"].items()}}
    return {"script": data, "options": options}, shape


def render(parts: Dict[str, Any]) -> bytes:
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count("__QA_WORKLOAD__") != 1 or template.count("__QA_OPTIONS__") != 1 or "://" in template:
        raise Refused("assets/k6-template.js lost its markers or holds a URL")
    workload_js = json.dumps(parts["script"], ensure_ascii=True, sort_keys=True, indent=2)
    options_js = json.dumps(parts["options"], ensure_ascii=True, sort_keys=True, indent=2)
    return template.replace("__QA_WORKLOAD__", workload_js).replace("__QA_OPTIONS__", options_js).encode("utf-8")


def positive(text: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,6}", text or ""):
        raise argparse.ArgumentTypeError("a whole number above 0")
    return int(text)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise Refused("arguments: " + message)


def main(argv: Sequence[str]) -> int:
    parser = _Parser(description=__doc__.splitlines()[0])
    parser.add_argument("--workload", required=True)
    parser.add_argument("--max-vus", required=True, type=positive)
    parser.add_argument("--max-duration-s", required=True, type=positive)
    parser.add_argument("--max-rps", type=positive)
    parser.add_argument("--out", required=True)
    root = Path.cwd()
    try:
        args = parser.parse_args(list(argv))
        doc = parse(read_workload(root, args.workload))
        parts, shape = workload(doc, args.max_vus, args.max_duration_s, args.max_rps)
        data = render(parts)
        written = write_script(root, args.out, data)
    except (Refused, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "written": written, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                      "input": "/work/.qa-input/performance/" + PurePosixPath(written).name, "workload": shape,
                      "limits": {"max_vus": args.max_vus, "max_duration_s": args.max_duration_s, "max_rps": args.max_rps}},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
