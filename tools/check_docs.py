"""Consistency checks for the Phase 1 and Phase 2 proposal documents and the brief.

Usage: python3 -I tools/check_docs.py <phase-1.md> <phase-2.md> <brief.md>
Prints problems and a count; exit code 1 if any were found, 2 on a usage error.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

problems = []

# A `§` inside the text of a link to a web page names a section of that page, not of these documents.
EXTERNAL_LINK = re.compile(r"\[[^\]]*\]\(https?://[^)\s]*\)")
BRIEF_ID = r"BR-\d+(?:\.[A-Za-z0-9]+)+"
BRIEF_DEFINITION = re.compile(r"^- \*\*(%s)\*\*" % BRIEF_ID, re.M)
CITED_ID = re.compile(r"(%s)(?:\([a-z]\))?" % BRIEF_ID)


def without_external_links(line):
    return EXTERNAL_LINK.sub("", line)


def heading_numbers(text):
    nums = set()
    cards = set()
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{2,4}) (\d+(?:\.\d+)*)\.? ", line)
        if m:
            nums.add(m.group(2))
        m = re.match(r"^#### ([A-P])\. ", line)
        if m:
            cards.add(m.group(1))
    return nums, cards


def split_cells(row):
    # split on pipes not preceded by a backslash
    parts = re.split(r"(?<!\\)\|", row)
    return parts[1:-1] if row.strip().endswith("|") else parts[1:]


def check_tables(name, text):
    lines = text.splitlines()
    in_code = False
    block = []

    def flush(block):
        if len(block) < 2:
            return
        header = split_cells(block[0][1])
        n = len(header)
        sep = block[1][1]
        if not re.match(r"^\|(\s*:?-+:?\s*\|)+\s*$", sep):
            problems.append(f"{name}:{block[1][0]}: table separator malformed")
        for ln, row in block[1:]:
            c = len(split_cells(row))
            if c != n:
                problems.append(f"{name}:{ln}: table row has {c} cells, header has {n}")

    for i, line in enumerate(lines, 1):
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("|"):
            block.append((i, line))
        else:
            flush(block)
            block = []
    flush(block)


def check_refs(name, text, own, own_cards, other, other_cards, other_label):
    in_code = False
    for ln, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        line = without_external_links(line)
        for m in re.finditer(r"§(\d+(?:\.\d+)*)(?: ([A-P])\b)?", line):
            num, card = m.group(1), m.group(2)
            before = line[max(0, m.start() - 12):m.start()]
            target_is_other = other_label in before
            if not target_is_other:
                # a range continuation "…–§7" inherits the label of its start
                prev = line[max(0, m.start() - 30):m.start()]
                if re.search(other_label + r" §[\d.]+–$", prev):
                    target_is_other = True
            nums, cards = (other, other_cards) if target_is_other else (own, own_cards)
            where = other_label if target_is_other else "this doc"
            if num not in nums:
                problems.append(f"{name}:{ln}: §{num} not found in {where}")
            if card and num == "6.4" and card not in cards:
                problems.append(f"{name}:{ln}: card §6.4 {card} not found in {where}")


def check_ids(name, text):
    defined = {}
    for prefix in ("D", "R", "A", "T", "F"):
        defined[prefix] = set(re.findall(rf"^\| ({prefix}\d+) \|", text, flags=re.M))
    in_code = False
    for ln, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for m in re.finditer(r"\b([DRATF])(\d{1,2})\b", line):
            ident = m.group(0)
            prefix = m.group(1)
            ctx = line[max(0, m.start() - 3):m.end() + 3]
            if prefix == "A" and re.search(r"API\d|A\d:", ctx):
                continue
            if prefix == "F" and "FND" in ctx:
                continue
            if ident not in defined[prefix]:
                problems.append(f"{name}:{ln}: {ident} referenced but not defined")
    # deviation numbers cited as "#N" next to "deviation" or "§22.2", defined by the rows of §22.2 and §22.3
    devs = deviation_numbers(text)
    if devs != list(range(1, len(devs) + 1)):
        problems.append(f"{name}: deviation numbers in §22.2 and §22.3 are not 1..n without gaps: {devs}")
    for ln, line in enumerate(text.splitlines(), 1):
        if "§22.2" in line or "§22.3" in line or "deviation" in line.lower():
            for m in re.finditer(r"#(\d+)", line):
                if int(m.group(1)) not in devs:
                    problems.append(f"{name}:{ln}: deviation #{m.group(1)} not defined")


def sections(text):
    """(section number, line number, line) for every line outside code blocks, under the nearest numbered heading."""
    number, in_code = "", False
    for ln, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^#{2,4} (\d+(?:\.\d+)*)\.? ", line)
        if m:
            number = m.group(1)
        elif re.match(r"^#{1,4} ", line):
            number = ""
        yield number, ln, line


def deviation_numbers(text):
    """The first-column numbers of the tables under §22.2 and §22.3, in order of appearance."""
    return [int(m.group(1)) for number, _ln, line in sections(text) if number in ("22.2", "22.3")
            for m in [re.match(r"^\| (\d+) \|", line)] if m]


def check_brief_coverage(name, text, brief):
    """Every ID the brief defines is in the first column of §22.1, alone or inside a range; every ID cited there is defined."""
    defined = BRIEF_DEFINITION.findall(brief)
    position = {ident: i for i, ident in enumerate(defined)}
    covered = set()
    for _number, ln, line in (s for s in sections(text) if s[0] == "22.1"):
        if not line.startswith("|") or re.match(r"^\|\s*:?-", line) or "Brief" in line.split("|")[1]:
            continue
        for token in (t.strip() for t in split_cells(line)[0].split(",")):
            ends = [m.group(1) for m in CITED_ID.finditer(token)]
            unknown = [e for e in ends if e not in position]
            for e in unknown:
                problems.append(f"{name}:{ln}: {e} is cited in §22.1 but the brief does not define it")
            if unknown:
                continue
            if len(ends) == 1 and CITED_ID.fullmatch(token):
                covered.add(ends[0])
            elif len(ends) == 2 and re.fullmatch(CITED_ID.pattern + "–" + CITED_ID.pattern, token):
                if position[ends[0]] > position[ends[1]]:
                    problems.append(f"{name}:{ln}: range {token} runs backwards in the brief's order")
                else:
                    covered.update(defined[position[ends[0]]:position[ends[1]] + 1])
            else:
                problems.append(f"{name}:{ln}: cannot read {token!r} as a brief ID or an ID range")
    for ident in defined:
        if ident not in covered:
            problems.append(f"{name}: {ident} is defined in the brief but missing from §22.1")


def main():
    if len(sys.argv) != 4:
        print("usage: check_docs.py <phase-1.md> <phase-2.md> <brief.md>")
        return 2
    p1, p2, pb = (Path(a) for a in sys.argv[1:])
    t1, t2, tb = (p.read_text(encoding="utf-8") for p in (p1, p2, pb))
    n1, c1 = heading_numbers(t1)
    n2, c2 = heading_numbers(t2)
    check_tables(p1.name, t1)
    check_tables(p2.name, t2)
    check_refs(p2.name, t2, n2, c2, n1, c1, "Phase 1")
    # Phase 1 refers to Phase 2 sections in its "Phase 2 response" columns; accept either doc
    in_code = False
    for ln, line in enumerate(t1.splitlines(), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for m in re.finditer(r"§(\d+(?:\.\d+)*)", without_external_links(line)):
            if m.group(1) not in n1 and m.group(1) not in n2:
                problems.append(f"{p1.name}:{ln}: §{m.group(1)} not found in either doc")
    check_ids(p2.name, t2)
    check_brief_coverage(p2.name, t2, tb)
    tops = sorted(int(n) for n in n2 if "." not in n)
    if tops != list(range(1, len(tops) + 1)):
        problems.append(f"{p2.name}: top-level section numbers not sequential: {tops}")
    for prob in problems:
        print(prob)
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
