from __future__ import annotations

import base64
import copy
import hashlib
import re
from html.parser import HTMLParser

import pytest

import test_bundle
import test_trace
from helpers import KERNEL, base
from qa import bundle, config, findings, gate, ledger, report, state
from qa.canonical import sha256_bytes
from qa.report import EXECUTIVE_ROW_LIMIT, INTEGRITY_ROWS_SHOWN
from qa.report.html import html_text, render_html
from qa.workspace import init_workspace
from test_compare import _execution, _result
from test_report import _gated_run, _tamper

# The brief's hostile values; the bidi override is written as its escape so this file shows no reordered text.
HOSTILE = ['<script>alert(1)</script>', '"><img src=x onerror=1>', '</style><style>*{}', 'javascript:alert(1)',
           'a\u202eb', 'tag\U000E0041', 'ctl\x07']
COMMIT = "a" * 40
LOCAL = "EXE-" + "0" * 25 + "D"
REMOTE = "EXE-" + "0" * 25 + "C"
SECTIONS = ["Executive summary", "Gate", "Executions", "Findings", "Traceability", "Integrity", "Re-execution", "Limitations"]
TAGS = {"html", "head", "meta", "title", "style", "body", "header", "main", "section", "h1", "h2", "h3", "h4", "p", "strong",
        "ul", "li", "table", "thead", "tbody", "tr", "th", "td", "br"}
CSP = "default-src 'none'; style-src 'sha256-%s'; img-src data:; base-uri 'none'; form-action 'none'"
HEADINGS = ("h2", "h3", "h4")


def template_style():
    template = (KERNEL.parent / "templates" / "report.html.tmpl").read_text(encoding="utf-8")
    return re.findall(r"<style>(.*?)</style>", template, re.S)


def shown(value):
    """``value`` as a reader sees it: the characters html_text marks become ``[U+XXXX]``."""
    hidden = re.compile("[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")
    return hidden.sub(lambda m: "[U+%04X]" % ord(m.group(0)), value)


class Page(HTMLParser):
    """What a browser's parser makes of the page: every start tag with its attributes, the text of each section, and
    each table as rows of cell text under the nearest heading (a ``<br>`` inside a cell reads as a newline)."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags, self.headings, self.tables, self.sections = [], [], [], {}
        self.style = []
        self._heading = None
        self._row = self._cell = self._text = None
        self._section = None
        self._in_style = False
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))
        if tag in HEADINGS:
            self._text = []
        elif tag == "style":
            self._in_style = True
        elif tag == "table":
            self.tables.append((self._heading, []))
        elif tag == "tr":
            self._row = []
            self.tables[-1][1].append(self._row)
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag in HEADINGS:
            self._heading = "".join(self._text)
            self.headings.append((tag, self._heading))
            if tag == "h2":
                self._section = self._heading
                self.sections[self._section] = []
            self._text = None
        elif tag == "style":
            self._in_style = False
        elif tag in ("td", "th"):
            self._row.append("".join(self._cell))
            self._cell = None

    def handle_data(self, data):
        if self._in_style:
            self.style.append(data)
        if self._text is not None:
            self._text.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._section is not None and self._text is None:
            self.sections[self._section].append(data)

    def table(self, heading, index=0):
        """The rows of the ``index``-th table under ``heading``, header row first."""
        return [rows for title, rows in self.tables if title == heading][index]

    def text(self, section):
        return "".join(self.sections[section])


def _local_run(ws, actor, argv, *, files=("stdout.log",), limitations=()):
    data = _execution(LOCAL)
    data["command"]["argv"] = list(argv)
    data["git"] = {"commit": COMMIT, "branch": "main", "dirty": False, "diff_hash": "sha256:" + "1" * 64}
    data["evidence_manifest"] = {"sha256": "sha256:" + "2" * 64, "files": list(files), "ids": {}}
    data["limitations"] = list(limitations)
    ledger.append(ws, actor, "execution.recorded", data)
    ledger.append(ws, actor, "result.recorded", _result(1, LOCAL, "t1", "passed"))
    ledger.append(ws, actor, "result.recorded", _result(2, LOCAL, "t2", "passed"))
    return LOCAL


@pytest.fixture
def st_with_values(trusted, actor, tmp_path):
    """Each value in a finding title, recommendation and evidence entry, a local argv, an evidence file name, an execution
    limitation, and the argv of an imported bundle's run."""
    def build(values):
        values = list(values)
        _local_run(trusted, actor, ["tool"] + values, files=values, limitations=values)
        for n, value in enumerate(values):
            findings.add(trusted, actor, base(title=value, evidence=[value], recommendation=value,
                                              refs={"rule": "r%d" % n, "location": "a/f%d.py" % n},
                                              component={"path": "a/f%d.py" % n, "criticality": "normal"}))
        test_bundle._import(trusted, actor, tmp_path, argv=["tool"] + values)
        gate.evaluate(trusted, actor, profile="change")
        return state.load(trusted)
    return build


@pytest.fixture
def st(trusted, actor):
    """A state with every section filled: a run on a commit, a finding, a traceability chain and a gate."""
    test_bundle._record(trusted, actor, "D", tests=(("t1", "passed"), ("t2", "failed")), status="failed")
    findings.add(trusted, actor, base(title="Critical one", recommendation="Fix the critical one"))
    test_trace.chain(trusted, actor)
    gate.evaluate(trusted, actor, profile="change")
    return state.load(trusted)


@pytest.fixture
def st_with_bundle(ws, actor, tmp_path):
    """Two imported bundles of the local run: the first disagrees on a test, the second matches."""
    test_bundle._record(ws, actor, "D")
    test_bundle._import(ws, actor, tmp_path, tests=(("t1", "passed"), ("t2", "failed")), status="failed")
    test_bundle._import(ws, actor, tmp_path)
    return state.load(ws)


# --------------------------------------------------------------------------- the brief's tests

def test_html_report_escapes_hostile_ledger_text(st_with_values):
    page = render_html(st_with_values(HOSTILE), project=HOSTILE[1])
    assert "<script" not in page.lower() and page.count("<style") == 1
    assert "\u202e" not in page and "\U000E0041" not in page and "[U+202E]" in page and "href=" not in page
    # The escaped text keeps the letters of an attribute-like value, so whether any element carries an event handler or a
    # source is read from the parsed tags rather than from the raw bytes.
    parsed = Page(page)
    assert {tag for tag, _ in parsed.tags} <= TAGS
    assert [tag for tag, attrs in parsed.tags if attrs] == ["html", "meta", "meta", "meta"]  # only the template's head
    assert not [name for _, attrs in parsed.tags for name, _ in attrs if name.startswith("on") or name in ("src", "href")]
    assert ["".join(parsed.style)] == template_style()  # no value reached the style element
    # Every value reads back as text, in each place it was recorded.
    values = [shown(v) for v in HOSTILE]
    findings_rows = parsed.table("Findings")[1:]
    assert [row[1] for row in findings_rows] == values and [row[-1] for row in findings_rows] == values
    assert sorted(row[-1] for row in parsed.table("Recommendations")[1:]) == sorted(values)
    local = next(row for row in parsed.table("Executions")[1:] if row[0] == LOCAL)
    assert local[-1].split("\n") == values and all(v in local[-2] for v in values)
    assert all(v in parsed.table("Imported executions")[1][-1] for v in values)
    limits = parsed.table("Limitations")[1:]
    assert [row for row in limits if row[0] == LOCAL] == [[LOCAL, v] for v in values]
    assert "<h1>QA report: %s</h1>" % html_text(HOSTILE[1]) in page and "<title>QA report: %s</title>" % html_text(HOSTILE[1]) in page


def test_html_report_csp_hash_matches_its_style(st):
    page = render_html(st)
    styles = re.findall(r"<style>(.*?)</style>", page, re.S)
    assert len(styles) == 1
    digest = base64.b64encode(hashlib.sha256(styles[0].encode("utf-8")).digest()).decode("ascii")
    metas = [dict(attrs) for tag, attrs in Page(page).tags if tag == "meta" and ("http-equiv", "Content-Security-Policy") in attrs]
    assert [m["content"] for m in metas] == [CSP % digest]
    assert page.index("Content-Security-Policy") < page.index("<style>")  # a policy in a meta element covers what follows it
    assert template_style() == styles and "$" not in styles[0]


def test_html_report_loads_nothing_external(st):
    page = render_html(st)
    for text in ("src=", "url(", "@import", "<link", "<iframe"):
        assert text not in page
    parsed = Page(page)
    assert not {"script", "img", "iframe", "object", "embed", "link", "form", "base", "a", "svg", "math"} & {t for t, _ in parsed.tags}
    assert "@font-face" not in "".join(parsed.style)


def test_html_report_is_byte_deterministic(st):
    before = copy.deepcopy(st)
    assert render_html(st) == render_html(st)
    assert render_html(st, project="Shop") == render_html(copy.deepcopy(st), project="Shop")
    assert st == before  # a pure function of the state
    page = render_html(st)
    assert page.endswith("</html>\n") and not page.endswith("\n\n")
    assert not re.search(r"RPT-\d|T\d\d:\d\d:\d\d", page)


def test_html_report_shows_bundle_comparisons(st_with_bundle):
    parsed = Page(render_html(st_with_bundle))
    assert [title for tag, title in parsed.headings if tag == "h3" and title.startswith("BUNDLE-")] == ["BUNDLE-001", "BUNDLE-002"]
    assert parsed.table("Comparisons", 0)[0] == ["Local", "Remote", "Commit", "Matching", "Differences"]
    first, second = parsed.table("Comparisons", 0)[1:], parsed.table("Comparisons", 1)[1:]
    assert [row[:4] for row in first] == [[LOCAL, REMOTE, COMMIT, "false"]]
    assert [row[:4] for row in second] == [[LOCAL, REMOTE, COMMIT, "true"]]
    assert "new_failures: 1" in first[0][4].split("\n") and "new_failures: 0" in second[0][4].split("\n")
    facts = dict(row for row in parsed.table("BUNDLE-001")[1:])
    assert facts["SHA-256"] == st_with_bundle["bundles"]["BUNDLE-001"]["sha256"] and facts["Imported executions"] == "1"
    imported = parsed.table("Imported executions", 0)
    assert imported[1][0] == REMOTE and imported[1][1] == "failed" and imported[1][-1] == "pytest -q"


def test_report_render_html_records_report_rendered(ws, actor, qa_cli):
    event = report.render(ws, actor, formats=("html",))
    folder = ws.path("reports", event["data"]["id"])
    assert list(event["data"]["files"]) == ["report.html"] and [p.name for p in folder.iterdir()] == ["report.html"]
    data = (folder / "report.html").read_bytes()
    st, project = state.load(ws), config.load_config(ws)["project"]["name"]
    assert event["data"]["files"]["report.html"] == sha256_bytes(data)
    assert data == render_html(st, project=project).encode("utf-8")
    code, out = qa_cli("report", "render", "--format", "html", "--format", "md")
    assert code == 0 and sorted(out["files"]) == ["report.html", "report.md"]
    assert out["files"]["report.html"] == event["data"]["files"]["report.html"]  # the same state, the same bytes


# --------------------------------------------------------------------------- html_text

@pytest.mark.parametrize("raw,want", [
    ("plain", "plain"), ("a<b>&\"'", "a&lt;b&gt;&amp;&quot;&#x27;"), ("tab\tnew\nline", "tab\tnew\nline"),
    ("\x00\x1f\r\x7f", "[U+0000][U+001F][U+000D][U+007F]"), ("\u202a\u202e\u2066\u2069", "[U+202A][U+202E][U+2066][U+2069]"),
    ("\U000e0000\U000e007f", "[U+E0000][U+E007F]"), ("\u2029\u202f\u2065\u206a\U000e0080\x80", "\u2029\u202f\u2065\u206a\U000e0080\x80"),
    (None, "None"), (3, "3"), ("&lt;", "&amp;lt;"),
])
def test_html_text_escapes_and_marks(raw, want):
    assert html_text(raw) == want


# --------------------------------------------------------------------------- sections

def test_html_report_sections_in_order_with_the_executive_summary_first(st):
    parsed = Page(render_html(st, project="Shop"))
    assert [title for tag, title in parsed.headings if tag == "h2"] == SECTIONS
    summary = parsed.text("Executive summary")
    g = st["gates"]["GATE-001"]
    assert "Verdict: %s (%s, profile %s, independence %s, state %s)" % (
        g["verdict"], g["id"], g["profile"], g["independence"], g["state_hash"]) in summary
    assert "Integrity failure" not in summary
    assert [row[2] for row in parsed.table("Recommendations")[1:]] == ["FND-001"]
    assert dict(row for row in parsed.table("Executive summary")[1:])["Unresolved findings"] == "1"
    assert [row[0] for row in parsed.table("Gate")[1:]] == [c["id"] for c in g["criteria"]]
    assert parsed.table("Traceability")[0] == ["Gap kind", "Count"]
    assert "No evidence bundles imported." in parsed.text("Re-execution")
    assert parsed.table("Executions")[1][0] == LOCAL and parsed.table("Executions")[1][-2] == "pytest -q"


def test_html_report_without_records_says_so(ws):
    parsed = Page(render_html(state.load(ws)))
    assert "No gate evaluation recorded; no verdict." in parsed.text("Executive summary")
    assert "No unresolved findings." in parsed.text("Executive summary")
    for section, text in (("Gate", "No gate evaluation recorded."), ("Executions", "No executions recorded."),
                          ("Findings", "No findings recorded."), ("Traceability", "No requirements or test cases recorded."),
                          ("Integrity", "The state fold found no violations.")):
        assert text in parsed.text(section)
    assert "QA report: unnamed project" in render_html(state.load(ws))


def test_html_report_carries_the_integrity_headline_and_caps_the_rows(st):
    problems = [{"kind": "modified", "where": "evidence/%d" % n, "detail": "changed"} for n in range(INTEGRITY_ROWS_SHOWN + 3)]
    parsed = Page(render_html(st, problems=problems))
    summary = parsed.text("Executive summary")
    assert summary.lstrip().startswith("Integrity failure: ledger verification found %d problems; see Integrity." % len(problems))
    assert "Recorded verdict, not current: " in summary and "Verdict: " not in summary.replace("Recorded verdict", "")
    assert len(parsed.table("Integrity")) == 1 + INTEGRITY_ROWS_SHOWN
    assert "3 more problems are not listed; run qa ledger verify." in parsed.text("Integrity")


def test_html_recommendations_are_capped_and_the_rest_counted(trusted, actor):
    total = EXECUTIVE_ROW_LIMIT + 2
    for n in range(total):
        findings.add(trusted, actor, base(title="Finding number %d" % n, refs={"rule": "r%d" % n, "location": "a/f%d.py" % n},
                                          component={"path": "a/f%d.py" % n, "criticality": "normal"}))
    parsed = Page(render_html(state.load(trusted)))
    assert len(parsed.table("Recommendations")) == 1 + EXECUTIVE_ROW_LIMIT
    assert "2 more unresolved findings are not listed here" in parsed.text("Executive summary")


def test_render_html_after_a_tamper_names_the_recorded_verdict_as_not_current(trusted, actor, py):
    exe = _gated_run(trusted, actor, py)
    _tamper(trusted, exe)
    event = report.render(trusted, actor, formats=("html",))
    summary = Page(trusted.path("reports", event["data"]["id"], "report.html").read_text(encoding="utf-8")).text("Executive summary")
    assert "Integrity failure: ledger verification found" in summary and "Recorded verdict, not current:" in summary


def test_a_shell_run_shows_its_line_and_an_argv_run_its_quoted_arguments(ws, actor):
    data = _execution(LOCAL)
    data["command"] = {"argv": ["make check && echo done"], "shell": True, "cwd": "."}
    ledger.append(ws, actor, "execution.recorded", data)
    test_bundle._record(ws, actor, "E", argv=("pytest", "-k", "a b"))
    rows = Page(render_html(state.load(ws))).table("Executions")[1:]
    assert [(row[0], row[-2]) for row in rows] == [(LOCAL, "make check && echo done"), ("EXE-" + "0" * 25 + "E", "pytest -k 'a b'")]


def test_a_project_name_is_text_in_the_title_and_heading(ws):
    page = render_html(state.load(ws), project="</title><b>x")
    assert "<title>QA report: &lt;/title&gt;&lt;b&gt;x</title>" in page and "<h1>QA report: &lt;/title&gt;&lt;b&gt;x</h1>" in page


def test_the_cli_offers_html():
    from qa import cli
    assert "html" in cli._REPORT_FORMATS and report._FILE_NAMES["html"] == "report.html" and "html" in report._FORMATS


def test_bundle_round_trip_carries_the_html_report(ws, actor, tmp_path):
    rid = report.render(ws, actor, formats=("html",))["data"]["id"]
    out = tmp_path / "with-html.tar.gz"
    bundle.create(ws, actor, out)
    assert "reports/%s/report.html" % rid in test_bundle._members(out)
    other = init_workspace(tmp_path / "other")[0]
    assert bundle.ingest(other, actor, out)["id"] == "BUNDLE-001"
