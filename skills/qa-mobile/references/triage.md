# Triage

One finding per problem, each written to `.qa/work/mobile/<name>.json` with Write and recorded with
`finding add --json .qa/work/mobile/<name>.json`. A problem the static check or your reading found is recorded even though no
device check ran: that is what `suspected` is for.

## How sure: `verification_status`

| Status | When |
|---|---|
| `suspected` | Read in the configuration, or listed by the static check. The status of every finding of this skill: no device check runs here |
| `confirmed` | Only when a report attached with `qa ingest` shows the behaviour (the traffic in cleartext, the file restored from a backup, the provider answering another app), and its EXE or bundle is in `evidence`. Never from reading, never from the static check, never from a person's word, never from a `blocked` or `not_run` run |
| `false_positive` | A file shows the setting does not apply (a later configuration overrides it); the finding names the file and line that show why. Never because someone says a device check went well |

## The finding template

```json
{
  "title": "android/app/src/main/res/xml/network_security_config.xml:3: the network security configuration permits cleartext traffic for every domain (MASVS-NETWORK-1)",
  "description": "The base-config element at android/app/src/main/res/xml/network_security_config.xml:3 sets cleartextTrafficPermitted to true, so every connection may use plain HTTP. Read from the configuration by the static check; MASTG-TEST-0236, the device check, was not run.",
  "category": "security",
  "severity_rationale": {"anchor": "SEC-M1", "modifiers": []},
  "component": {"path": "android/app/src/main/res/xml/network_security_config.xml", "criticality": "normal"},
  "verification_status": "suspected",
  "evidence": ["android/app/src/main/res/xml/network_security_config.xml:3", ".qa/work/mobile/mobile-static.sarif"],
  "reproduction_steps": ["Open android/app/src/main/res/xml/network_security_config.xml at line 3", "Read the cleartextTrafficPermitted attribute of base-config"],
  "impact": "Traffic sent over HTTP can be read and changed by anyone on the network path.",
  "probable_cause": "android/app/src/main/res/xml/network_security_config.xml:3 base-config sets cleartextTrafficPermitted=\"true\"",
  "recommendation": "Set cleartextTrafficPermitted to false in base-config, and allow cleartext only in a domain-config for a host that needs it, if any; nothing was changed here.",
  "refs": {"rule": "cleartext-traffic", "location": "android/app/src/main/res/xml/network_security_config.xml", "masvs": ["MASVS-NETWORK-1"], "mastg": ["MASTG-TEST-0235"], "maswe": ["MASWE-0026"], "cwe": ["CWE-319"]}
}
```

- `title` and `probable_cause` start with `path:line`, the path from the project root (`android/app/src/main/AndroidManifest.xml:8`,
  never `AndroidManifest.xml:8`), the line as the check's `at` gives it or as Read numbers it, never a line counted by eye.
- `refs.rule` is the check's rule id, or `static-review` for a problem you read that no rule lists. `refs.location` and
  `component.path` are the file. Leave out `key_attributes`: the container leg's result for the same rule in the same file is
  then a new sighting of this finding, not a second one. Two results of one rule in one file (two exported activities) are
  one finding: list each element with its line in the description.
- `refs.masvs`, `refs.mastg`, `refs.maswe` and `refs.cwe` are copied from the check's result (`references/masvs-mapping.md`).
- Name a device test only as not run ("MASTG-TEST-0216, the device check, was not run"), never with an outcome.
- The recommendation says what to change; you change nothing.

## Rules and what they become

| Rule | Anchor | What a person checks beyond the rule | Device check that would decide it |
|---|---|---|---|
| `cleartext-traffic` | `SEC-M1` | whether a domain that allows cleartext needs it | `MASTG-TEST-0236` |
| `exported-without-permission` | `SEC-M1`; `SEC-H1` only when the code shows the component returns sensitive data or performs a sensitive action | whether the component exposes sensitive functionality (MASTG-TEST-0364, 0365, 0366, 0355) | `MASTG-TEST-0356` for providers; none for the others |
| `backup-enabled` | `SEC-L1` | what the app stores in the files a backup takes | `MASTG-TEST-0216` |
| `debuggable` | `SEC-M1` | whether this manifest is the release build's | none |
| `ats-arbitrary-loads` | `SEC-M1` | whether the app's purpose needs arbitrary loads (a browser) | `MASTG-TEST-0236` |
| `ats-insecure-exception` | `SEC-M1` | whether the domain's exception is justified | `MASTG-TEST-0236` |
| `get-task-allow` | `SEC-L1` | whether this entitlements file signs the distributed build | none |

The anchors are rated in the `SEC` column of `.claude/skills/qa-mobile/scripts/_kernel/policies/severity-rubric.json`.

## Negative evidence: what is not a finding

- The launcher activity (an intent filter with the `MAIN` action and the `LAUNCHER` category) is exported by design. The
  check does not list it; do not report it.
- A component protected by a permission this manifest declares at a `signature` level is protected. The check does not list
  it; do not report it.
- A component with `android:exported="false"` is not reachable from other apps.
- The check's `notes` (a permission declared elsewhere, a key that iOS ignores beside another) are things to mention, not
  findings, unless your reading of the files shows a problem.

## Two records of one problem

A problem you recorded by reading and the static check's result for the same rule and file are one record when you recorded
yours first with the template above (no `key_attributes`). If a second record opened anyway (a `static-review` finding of
yours and the check's result for the same element), keep yours and link the other in two calls:

1. `finding update <KEEP> --json <file>`: add to KEEP's `evidence` what the other adds (its EXE). The link itself moves
   nothing.
2. `finding state <OTHER> duplicate --reason "<why they are one problem>" --duplicate-of <KEEP>`.

## A report from a device run

Only `qa ingest` brings one in (`references/dynamic-checks.md`). Once a report is attached, read its results by the EXE or
bundle id the ingest printed; a finding the report shows becomes `confirmed` with that id in `evidence`. A report the user
describes but does not give is not attached: ask for the file.
