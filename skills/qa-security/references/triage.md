# Triage

A tool's alert is a lead, not a finding. Triage decides, per alert, whether the weakness is reachable from an input an attacker controls, what it allows, and how sure the evidence makes you. Read alerts from `sarif_triage.py`'s output (`.qa/work/security/triage-<report>.json`): it lists rule, CWE and location, with no snippet and every secret masked.

## The order

1. **Group.** One finding per weakness at one location, not per alert. ZAP reports a missing header once per URL it crawled: that is one finding on the response headers, with the URLs as its scope. Several rules on one line of code are one finding when they describe one cause.
2. **Locate the source.** Where does the value come from? A request parameter, header, cookie or body (attacker-controlled); a configuration file the operator writes (not attacker-controlled, but a secret there is still exposed to everyone who reads the repository); a constant.
3. **Follow it to the sink.** Read the code path from the source to the call the rule names (`execute`, a template render, `open`, a response header). Note every check on the way: a parameterized query, an allow-list, an escape, a resolved path compared with its base directory. A check that covers the value ends the triage as `false_positive`, with the check's file and line as the reason.
4. **Reachability.** Is the code on a route, a handler, a job or an import path that runs? A function nobody calls is a lower severity, not a non-finding: say "not reachable from any route found" and how you looked.
5. **Impact.** What the weakness allows, in one sentence, by class: "a request parameter changes the SQL statement of the product listing (CWE-89)". Never write the input that would do it.
6. **Severity** from the suite's rubric (`.claude/skills/qa-security/scripts/_kernel/policies/severity-rubric.json`), by its anchors, not by the tool's level.
7. **Status** by the evidence, below.

## How sure: `verification_status`

| Status | When |
|---|---|
| `suspected` | Read in the code or reported by a static tool, not shown by any execution. The default for every static finding |
| `probable` | A tool observed a symptom of it in a recorded execution (a ZAP passive alert on a response, an OSV advisory on the pinned version), but the weakness itself was not shown |
| `confirmed` | A recorded execution showed the behaviour itself: the run's id, its status and the evidence file that shows it are in the finding. Never from reading code alone, never from a run that was `blocked` or `not_run` |
| `false_positive` | A check covers it; the finding names the check's file and line |
| `not_reproducible` | An execution tried and did not show it |

A finding the static review found and a tool also reported keeps both: the tool's execution id in `evidence`, and `suspected` or `probable` by the table. A tool's alerts the kernel ingested open provisional findings of their own; triage them with `finding update` (rubric anchor, status), and link a duplicate of a static finding with `finding dedupe`.

## Secrets

- The finding names the file, the line and the rule (`stripe-access-token`, CWE-798). Never the value, any part of it, a hash of it or its length.
- Gitleaks runs with `--redact`; the kernel masks known secret shapes in everything it stores; `sarif_triage.py` masks long mixed tokens. Do not undo any of it: never open `settings.py` or `.env` to print a line, never `grep` for the value.
- Remediation: rotate the credential (it is exposed to everyone who read the repository) and load it from the environment or a secret store. Rotation is the owner's step, not this skill's.

## Dependencies

- OSV-Scanner's result names the package, the pinned version and the advisory. Reachability: is the vulnerable function used? Name the import and the call, or say that no call to the affected API was found and how you looked. An advisory on an unused feature is still a finding (the pinned version is shipped), at a lower severity.
- The remediation is the first fixed version the advisory names; read it from the stored report, never from memory.
- **Offline, the only other check is lockfile consistency**: does every pinned version in the manifest match the lockfile, is anything pinned in one and missing from the other, are there unpinned ranges (`>=`, `~=`, no version) that let a build pick another release? Say what you compared.
- **The dependency-existence check is opt-in and is not run in this version.** Asking a package index whether each name exists (to catch names a model invented, or typo-squats) sends the dependency list to a third party: it needs a `data-egress` grant from the user and a recorded execution, and this skill neither asks for that grant nor runs it. Say it was not run, and why, in the coverage.

## Static code findings

- Each finding cites the file and the line range, the source and the sink, and the CWE. Describe the class and the place, never a working input.
- `refs`: `{"rule": "<the tool's rule id, or static-review>", "location": "<path>", "cwe": ["CWE-n"], "owasp_top10_2025": ["Ann:2025"]}`, and `owasp_api_2023` / `asvs_5_0` when they apply (`references/owasp-cwe-mapping.md`).

## Running-application findings (ZAP)

- A passive alert (a header, a cookie attribute, an error page) is about a response: the component is the endpoint (`GET /session`), the evidence is the run's report.
- An active-scan alert names the parameter and the class, never the attack string ZAP sent: ZAP's report keeps it; the finding points to the report, it does not copy the request.
- A ZAP alert on a response from the project's own application, started in that run, on `127.0.0.1`, is `probable` for the class it names. `confirmed` needs the stored evidence to show the behaviour itself, and the finding says which file shows it.

## The finding file

`finding add --json .qa/work/security/<name>.json` takes one finding; write the file with your file tool, then add it:

```json
{
  "title": "<file or endpoint: the weakness by class, under 120 characters>",
  "description": "<source, sink and the missing check, by class; say whether only the static reading shows it> (CWE-<n>)",
  "category": "security",
  "severity_rationale": {"anchor": "<SEC-C1 | SEC-C2 | SEC-H1 | SEC-M1 | SEC-L1 | SEC-I1>", "modifiers": []},
  "component": {"path": "<file>", "endpoint": "<METHOD /path, when it is about a route>", "criticality": "<critical|high|normal|low>"},
  "verification_status": "<suspected | probable | confirmed: the table above>",
  "evidence": ["<EXE of the run that reported or showed it, when there is one>", "<path>:<line>"],
  "reproduction_steps": ["<the qa run command that reported it, or: read <path>:<lines>>"],
  "impact": "<what the class allows here, in one sentence, with no input that would do it>",
  "probable_cause": "<path>:<line> <one sentence>",
  "recommendation": "<the fix; nothing was changed here>",
  "refs": {"rule": "<the tool's rule id, or static-review>", "location": "<path, no line number>", "cwe": ["CWE-<n>"],
           "owasp_top10_2025": ["A<nn>:2025"]},
  "key_attributes": {"rule": "<the same rule>", "cwe": "CWE-<n>", "path": "<path>"}
}
```

- The anchors are the suite's (`.claude/skills/qa-security/scripts/_kernel/policies/severity-rubric.json`): SEC-C1 remotely exploitable without authentication (injection into a query, traversal to any file, a signature that is not checked); SEC-C2 a valid production secret exposed; SEC-H1 exploitable with low-privilege access; SEC-M1 needs user interaction or specific conditions (reflected cross-site scripting, a traceback in an error page, a permissive cross-origin policy); SEC-L1 a hardening gap with no demonstrated path (a missing header or cookie attribute, a vulnerable dependency not shown reachable); SEC-I1 a note. A generated test credential in a lab is not a valid production secret: anchor it by what it signs.
- `refs.location` is a path with no line number; the line goes in `probable_cause` and `evidence`. `probable_cause` must cite a `path:line`, an `EV-` or an `EXE-` id, or the kernel refuses it.
- `refs` also takes `owasp_api_2023` and `asvs_5_0` lists when they apply (`references/owasp-cwe-mapping.md`).
- The kernel fills the id, the state, the priority, the source and the fingerprint. An `EXE-` id in `evidence` must be a real execution from `state show executions`. If `finding add` refuses, read `error`, correct the JSON and retry once.
