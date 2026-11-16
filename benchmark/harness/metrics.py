"""The benchmark's aggregation: per scenario and arm, what the runs cost and how their grades came out.

``aggregate(grades, runs)`` takes the grade documents (``grade_run.py``) and the runs' ``end`` lines (a judge's lines,
``engine: "judge"``, are not runs and are left out) and groups the runs by ``(case_id, config)``. Each group holds:

- ``runs`` (end lines), ``graded`` (runs with a grade) and ``run_ids``;
- ``cost``: ``sum``, ``mean``, ``min`` and ``max`` of the known costs, and how many are ``unknown`` (never counted as 0);
  ``judge_cost``: the sum of the group's known judge costs and how many are unknown;
- ``tokens``: the mean ``input``, ``output``, ``cache_read`` and ``cache_creation`` over the runs that report each;
- ``duration_ms``: ``mean`` and ``max``;
- ``metrics``: for every number (or null) at the top level of each metric (``fabrication.unbacked``, ``recall.recall``,
  ``precision.precision`` …), ``{"values", "n", "mean", "stdev"}`` over the graded runs, in run order: ``values`` holds
  null where a run has no such number, ``n`` counts the numbers, and ``stdev`` is the sample standard deviation (n - 1;
  null for fewer than two values). A metric that could not be computed in some run also gets ``<metric>.errors``, its
  reason per graded run;
- ``hard``: per hard metric, ``{"holds", "pass", "fail", "ungraded"}``; it holds only when every run of the group was
  graded and passed it.

``render_md(metrics)`` writes the same as Markdown tables. Precision is labelled a lower bound: a finding no key issue
matches may still be real.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

TOKENS = ("input", "output", "cache_read", "cache_creation")
LOWER_BOUND = ("precision.precision",)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _stdev(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _known_cost(record: Mapping[str, Any]) -> Optional[float]:
    return record["cost_usd"] if record.get("cost_unknown") is False and _number(record.get("cost_usd")) else None


def _group(scenario: str, arm: str, ends: Sequence[Mapping[str, Any]], by_id: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    ids = [e["run_id"] for e in ends]
    graded = [by_id[i] for i in ids if i in by_id]
    costs = [c for c in (_known_cost(e) for e in ends) if c is not None]
    judges = [g.get("judge") or {} for g in graded]
    tokens = {}
    for name in TOKENS:
        counts = [e["tokens"][name] for e in ends if isinstance(e.get("tokens"), dict) and _number(e["tokens"].get(name))]
        tokens[name] = _mean(counts)
    durations = [e["duration_ms"] for e in ends if _number(e.get("duration_ms"))]
    fields: List[str] = []
    errors: List[str] = []
    for g in graded:
        for metric, value in sorted((g.get("metrics") or {}).items()):
            if not isinstance(value, dict):
                continue
            if isinstance(value.get("error"), str) and metric not in errors:
                errors.append(metric)
            for field, number in sorted(value.items()):
                name = "%s.%s" % (metric, field)
                if (number is None or _number(number)) and name not in fields:
                    fields.append(name)
    table: Dict[str, Any] = {}
    for name in sorted(fields):
        metric, field = name.split(".", 1)
        values = [((g.get("metrics") or {}).get(metric) or {}).get(field) for g in graded]
        values = [v if _number(v) else None for v in values]
        numbers = [v for v in values if v is not None]
        table[name] = {"values": values, "n": len(numbers), "mean": _mean(numbers), "stdev": _stdev(numbers)}
    for metric in sorted(errors):
        table["%s.errors" % metric] = {"values": [((g.get("metrics") or {}).get(metric) or {}).get("error") for g in graded]}
    names = sorted({name for g in graded for name in (g.get("hard") or {})})
    hard = {}
    for name in names:
        passed = sum(1 for g in graded if (g.get("hard") or {}).get(name) == "pass")
        failed = sum(1 for g in graded if (g.get("hard") or {}).get(name) == "fail")
        ungraded = len(ids) - passed - failed
        hard[name] = {"holds": passed == len(ids) and len(ids) > 0, "pass": passed, "fail": failed, "ungraded": ungraded}
    return {"scenario": scenario, "arm": arm, "runs": len(ids), "graded": len(graded), "run_ids": ids,
            "cost": {"sum": sum(costs) if costs else None, "mean": _mean(costs), "min": min(costs) if costs else None,
                     "max": max(costs) if costs else None, "unknown": len(ends) - len(costs)},
            "judge_cost": {"sum": sum(c for c in (_known_cost(j) for j in judges if j.get("called", True)) if c is not None),
                           "unknown": sum(1 for j in judges if j.get("cost_unknown") is True)},
            "tokens": tokens, "duration_ms": {"mean": _mean(durations), "max": max(durations) if durations else None},
            "metrics": table, "hard": hard}


def aggregate(grades: Sequence[Mapping[str, Any]], runs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_id = {g["run_id"]: g for g in grades if isinstance(g, Mapping) and isinstance(g.get("run_id"), str)}
    groups: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    seen = set()
    for end in runs:
        if not isinstance(end, Mapping) or end.get("engine") == "judge" or end.get("event", "end") != "end" \
                or not isinstance(end.get("run_id"), str) or end["run_id"] in seen:
            continue
        seen.add(end["run_id"])
        groups.setdefault((str(end.get("case_id")), str(end.get("config"))), []).append(end)
    return {"groups": [_group(scenario, arm, ends, by_id) for (scenario, arm), ends in sorted(groups.items())]}


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        text = ("%.4f" % value).rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    return str(value)


def render_md(metrics: Mapping[str, Any]) -> str:
    lines = ["# Benchmark metrics", ""]
    for group in metrics.get("groups", []):
        cost, duration, tokens = group["cost"], group["duration_ms"], group["tokens"]
        lines += ["## %s: %s" % (group["scenario"], group["arm"]), "",
                  "| runs | graded | cost sum | cost mean | cost min | cost max | cost unknown | judge cost | duration mean ms "
                  "| duration max ms |", "|---|---|---|---|---|---|---|---|---|---|",
                  "| %s |" % " | ".join(_fmt(v) for v in (group["runs"], group["graded"], cost["sum"], cost["mean"], cost["min"],
                                                          cost["max"], cost["unknown"], group["judge_cost"]["sum"],
                                                          duration["mean"], duration["max"])), "",
                  "| tokens input | tokens output | tokens cache read | tokens cache creation |", "|---|---|---|---|",
                  "| %s |" % " | ".join(_fmt(tokens[name]) for name in TOKENS), "",
                  "| metric | n | mean | stdev | values |", "|---|---|---|---|---|"]
        for name, row in group["metrics"].items():
            if name.endswith(".errors"):
                lines.append("| %s | - | - | - | %s |" % (name, ", ".join(_fmt(v) for v in row["values"])))
                continue
            label = name + (" (lower bound)" if name in LOWER_BOUND else "")
            lines.append("| %s | %s | %s | %s | %s |" % (label, row["n"], _fmt(row["mean"]), _fmt(row["stdev"]),
                                                          ", ".join(_fmt(v) for v in row["values"])))
        lines += ["", "| hard metric | holds in every run | pass | fail | ungraded |", "|---|---|---|---|---|"]
        for name, row in group["hard"].items():
            lines.append("| %s | %s | %s | %s | %s |" % (name, _fmt(row["holds"]), row["pass"], row["fail"], row["ungraded"]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
