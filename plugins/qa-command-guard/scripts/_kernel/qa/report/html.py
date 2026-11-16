"""The self-contained HTML report: one page, one inline style element allowed by its hash, nothing that runs or loads.

``render_html`` is a pure function of the state, like ``render_markdown``: no clock and no report ID, so the same state
gives the same bytes. What each section says comes from ``_rows``, the builders the Markdown reports use too; this
module only writes their blocks as HTML. Every value from the ledger or the config reaches the page through
``html_text`` as element text. No value is ever placed in an attribute, a URL, the style element or a script, and the
page renders no link, so a value shaped like markup or a URL stays text.

The content security policy sits in a meta element before the style element. It names the SHA-256 of that element's text,
computed here from the template, so the template's style is the only one a browser applies; the policy's other
directives refuse every fetch, base URL and form submission.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
from string import Template
from typing import Optional, Sequence

from .. import resource_dir
from . import _rows

__all__ = ["html_text", "render_html"]

# C0 controls but tab and newline, DEL, the bidi embeddings and overrides, the bidi isolates, and the tag characters.
_MARKED = re.compile("[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")
_STYLE = re.compile(r"<style>(.*?)</style>", re.S)


def html_text(value: object) -> str:
    """``value`` as element text: HTML-escaped, with each control, bidi or tag character shown as ``[U+XXXX]``."""
    return _MARKED.sub(lambda found: "[U+%04X]" % ord(found.group(0)), html.escape(str(value), quote=True))


def _cell(value: object) -> str:
    """One table cell's content: nothing for None, true or false for a boolean, one line per item of a list."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "<br>".join(_cell(item) for item in value)
    return html_text(value)


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join("<th>%s</th>" % html_text(name) for name in header)
    body = "".join("<tr>%s</tr>\n" % "".join("<td>%s</td>" % _cell(value) for value in row) for row in rows)
    return "<table>\n<thead><tr>%s</tr></thead>\n<tbody>\n%s</tbody>\n</table>" % (head, body)


def _p(text: object) -> str:
    return "<p>%s</p>" % html_text(text)


def _h(level: int, text: object) -> str:
    return "<h%d>%s</h%d>" % (level, html_text(text), level)


def _block(block: _rows.Block) -> str:
    """One block as HTML: a sentence as a paragraph, a label in ``<strong>``, a table or a line as a table, bullets as a list."""
    if isinstance(block, str):
        return _p(block)
    if isinstance(block, _rows.Labelled):
        return "<p><strong>%s</strong> %s</p>" % (html_text(block.label), html_text(block.text))
    if isinstance(block, _rows.Table):
        return _table(block.header, block.rows)
    if isinstance(block, _rows.Line):
        return _table(block.header, [block.cells])
    if isinstance(block, _rows.Bullets):
        return "<ul>\n%s</ul>" % "".join("<li>%s</li>\n" % html_text(item) for item in block.items)
    if isinstance(block, _rows.Heading):
        return _h(block.level, block.text)
    raise TypeError("not a report block: %r" % (block,))


def _section(blocks: Sequence[_rows.Block]) -> str:
    return "\n".join(_block(block) for block in blocks)


def _summary(st: dict, gate: Optional[dict], problems: Sequence[dict]) -> str:
    """The executive summary: the same headline, verdict, counts, recommendations and follow-up as ``executive.md``."""
    found = _rows.headline(st, gate, problems)
    blocks = ([found] if found is not None else []) + [_rows.verdict(gate, problems), _rows.executive_summary(st, gate),
                                                        _rows.Heading(3, "Recommendations")]
    recommendations, more = _rows.recommendations(st)
    blocks.append(recommendations)
    if more:
        blocks.append(_rows.more_findings(more, "Findings"))
    blocks += [_rows.Heading(3, "Gate follow-up"), _rows.follow_up(gate)]
    return _section(blocks)


def _style_hash(source: str) -> str:
    """The CSP source for the template's one style element: base64 of the SHA-256 of its exact text."""
    styles = _STYLE.findall(source)
    if len(styles) != 1 or "$" in styles[0]:  # a "$" would change in substitution, and the hash would name other text
        raise ValueError("the HTML report template must hold exactly one style element, with no $ in it")
    return base64.b64encode(hashlib.sha256(styles[0].encode("utf-8")).digest()).decode("ascii")


def render_html(st: dict, *, project: Optional[str] = None, problems: Sequence[dict] = ()) -> str:
    source = (resource_dir("templates") / "report.html.tmpl").read_text(encoding="utf-8")
    gate = _rows.latest_gate(st)
    text = Template(source).substitute(
        style_hash=_style_hash(source), project=html_text(_rows.UNNAMED if project is None else project),
        summary=_summary(st, gate, problems), gate=_section(_rows.gate_section(gate)),
        executions=_section(_rows.execution_section(st, wide=True)), findings=_section(_rows.finding_section(st, wide=True)),
        traceability=_section(_rows.traceability_section(st, gate)), integrity=_section(_rows.integrity_section(st, gate, problems)),
        reexecution=_section(_rows.reexecution_section(st)), limitations=_section(_rows.limitation_section(st)))
    return text.rstrip("\n") + "\n"
