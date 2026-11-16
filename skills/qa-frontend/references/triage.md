# Triage

One finding per problem, each written to `.qa/work/frontend/<name>.json` with Write and recorded with `finding add --json .qa/work/frontend/<name>.json`. A problem the static scan or your reading found is recorded even when every browser run was `blocked`: that is what `suspected` is for.

## How sure: `verification_status`

| Status | When |
|---|---|
| `suspected` | Read in the source, or listed by the static scan, and not shown by any browser run. The default for every finding of this skill that no browser run backs |
| `probable` | A recorded browser run showed a symptom on the page (a console error on the route, a Lighthouse audit failing), but not the cause the finding names |
| `confirmed` | A recorded browser run whose status is `passed` or `failed` showed the behaviour itself: its EXE is in `evidence`, with the page, the viewport and the test case or axe rule that shows it. Never from reading, never from the static scan alone, never from a `blocked` or `not_run` run |
| `false_positive` | The rendered page does not show it (a label added by a script, a width overridden by a later rule); the finding names the run or the line that shows why |
| `not_reproducible` | A browser run tried and did not show it |

## The finding template

```json
{
  "title": "site/index.html:19: the hero image has no text alternative (WCAG 1.1.1)",
  "description": "The img element at site/index.html:19 has no alt attribute and no other accessible name. Read from the source by the static scan, not shown in a browser.",
  "category": "accessibility",
  "severity_rationale": {"anchor": "A11Y-M1", "modifiers": []},
  "component": {"path": "site/index.html", "criticality": "normal"},
  "verification_status": "suspected",
  "evidence": ["site/index.html:19", ".qa/work/frontend/static-ui.sarif"],
  "reproduction_steps": ["Open site/index.html at line 19", "Load / in a browser with a screen reader and move to the image"],
  "impact": "A screen reader announces the image by its file name or not at all.",
  "probable_cause": "site/index.html:19 the img element has no alt attribute",
  "recommendation": "Give the image an alt text that says what it shows, or alt=\"\" if it is decorative; nothing was changed here.",
  "refs": {"rule": "img-without-alt", "location": "site/index.html", "wcag": ["1.1.1"]}
}
```

- `title` and `probable_cause` start with `path:line`: the line as the scan's `at` gives it or as Read numbers it, never a line counted by eye.
- `refs.rule` is the scan's rule id, or `static-review` for a problem you read that no rule lists. `refs.location` and `component.path` are the file. Leave out `key_attributes`: the container leg's result for the same rule in the same file is then a new sighting of this finding, not a second one.
- `refs.wcag` lists WCAG 2.2 criteria by number (`"1.4.10"`), `refs.cwe` a CWE (`"CWE-20"`), `refs.wcag_related` a criterion the problem touches but does not fail as written.
- The recommendation says what to change; you change nothing.

## Static-scan rules and what they become

| Scan rule | Category | `refs` | Rubric anchor | What decides it |
|---|---|---|---|---|
| `img-without-alt` | accessibility | `wcag: ["1.1.1"]` | `A11Y-M1`; `A11Y-H1` when the image is the only name of a control on the main journey | axe `image-alt`; a person for the quality of the text |
| `click-handler-on-non-interactive` | accessibility | `wcag` as the scan lists it (`2.1.1`, `4.1.2`) | `A11Y-H1` when the control is on the main journey (it cannot be used without a mouse); `A11Y-M1` otherwise | the keyboard check of the checks leg |
| `input-without-label` | accessibility | `wcag: ["1.3.1", "3.3.2", "4.1.2"]` | `A11Y-M1` | axe `label` |
| `email-input-not-typed` | functional | `cwe: ["CWE-20"]`, `wcag_related: ["3.3.1"]` | `FUNC-M2` | the email check of the checks leg |
| `fixed-width-over-375` | accessibility | `wcag: ["1.4.10"]` | `A11Y-M1` | the overflow check at 375x812 |

## Two records of one problem

The email field's finding is `functional`. The container leg records every result in the run's category, `accessibility`, so its result for that field opens a second finding: the open one with the same rule and file and the anchor `TOOL-PROVISIONAL` (`state show findings`). An axe finding and your static finding for the same problem are two records too.

Keep one record: the one a run confirmed, else yours (it holds the file and the line). Link the other to it in two calls:

1. `finding update <KEEP> --json <file>`: add to KEEP's `evidence` what the other adds (its EXE, your `path:line`). The link itself moves nothing.
2. `finding state <OTHER> duplicate --reason "<why they are one problem>" --duplicate-of <KEEP>`.

`finding dedupe` links only the pairs it proposes itself (two records with the same file, the same CWE and overlapping lines). It never proposes these pairs and refuses them (`not_a_duplicate`), so do not use it here.

## Problems only a rendered page shows

Read the page's scripts and styles for these. Each is a `suspected` finding with the file and the lines of its cause until a browser run shows it. The anchor's column must match the category's (`<skill-dir>/scripts/_kernel/policies/severity-rubric.json`, `columns`): `accessibility` takes an `A11Y-` anchor, `functional` and `usability` a `FUNC-` anchor; `reliability` takes a `PERF-` anchor, so a broken page state is `functional`.

| Problem | Category | `refs` | Anchor | What shows it |
|---|---|---|---|---|
| An error thrown on one route (a script reads an element that is not on that page) | functional | `rule: "static-review"` | `FUNC-M1` | the console check of the checks leg on that route; Lighthouse `errors-in-console` |
| A list that renders nothing when the data is empty | usability | `rule: "static-review"` | `FUNC-L1` | the empty-state check (`references/states.md`) |
| A loading indicator that stays after a failed request, with no error message | functional | `rule: "static-review"`, `wcag_related: ["4.1.3"]` | `FUNC-M1` | the error-state check |
| Text colour below 4.5:1 on its background (3:1 for large text) | accessibility | `rule: "static-review"`, `wcag: ["1.4.3"]` | `A11Y-M1` | axe `color-contrast` on the rendered page. A ratio worked out from two colours in a style sheet is a lead: the cascade decides the colours |
| No visible focus style on a control | accessibility | `rule: "static-review"`, `wcag: ["2.4.7"]` | `A11Y-M1` | a person moving through the page with Tab |

## Tool results and the findings they open

- The static leg (SARIF): one provisional finding per result, matched to yours by rule and file when you recorded yours first.
- axe (`axe` report): each violation opens a finding of the run's category, `accessibility`, with the axe rule as `refs.rule` and the page path (`/`) as its place; it carries no WCAG reference of its own. Read the rule's WCAG tags in the stored report (`.qa/evidence/<EXE>/reports/axe.json`), add them with `finding update <FND> --json <file>` (`refs.wcag`, the anchor, `confirmed` with the EXE and your static finding's `path:line` in `evidence`), then make your static finding for the same problem its duplicate (two records of one problem, above).
- Playwright (`junit` report): each test case is a result of the run; a failed one names the page, the viewport and what failed. Record one finding per problem (not per viewport), `confirmed` with the EXE and the failed test case names in `evidence`.
- Lighthouse (`lighthouse` report): the scores and lab metrics are measurements of the run, never findings and never a verdict. A failing audit you report is a finding of its own, `probable` with the EXE.
