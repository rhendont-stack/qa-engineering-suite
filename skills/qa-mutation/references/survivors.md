# Survivors, smells and findings

## Reading a survivor

A survivor is a mutant the tests ran against and did not notice (status `Survived`). For each one, read the line it changes and decide which kind it is.

| Kind | How to tell | What to write |
|---|---|---|
| A real gap | Some input would make the original and the mutant return different results, and no test uses such an input or checks that result | The function, the change (`before -> after`), one input that tells them apart with both results, and the assertion that would kill it |
| Equivalent | No input can tell them apart. Examples: a change in dead code, `x * 1` against `x`, a boundary on a value that can never reach it, a log message | Why no input distinguishes them, in one sentence. An equivalent mutant is not a gap, and it is never recorded as a finding |
| Not worth a test | It changes only a message, a log line or a default no caller uses | Say so; recommend nothing |

When you are unsure whether a mutant is equivalent, it is a gap: name the input you tried. Work out an input that tells them apart by evaluating both expressions by hand for that input. Never "check" one by editing the code or adding a test here.

A `NoCoverage` mutant is in code no selected test reaches: the code is untested, not weakly tested. Report it per function.

Many survivors in one function usually share one cause, such as only round inputs or only one branch. Group them by function and cause, and name the one test that would kill the group.

## Coverage against the score

Show both, side by side. Never let one stand for the other:

- High line coverage with survivors: the lines run, but nothing checks what they compute. The survivors show where.
- Low coverage: the `NoCoverage` mutants are the same story seen from the mutation side.
- A number the user quotes ("coverage is 100%") is a claim. Quote the measured value next to it. If the two differ, say so.

## Test smells

`scripts/test_smells.py <test files>` flags six patterns. Each hit is a lead. Read the test, then confirm it or clear it with a reason.

| Rule | Confirm when | Clear when |
|---|---|---|
| `assertion-free` | The test calls code and checks nothing; only an exception would fail it | The framework checks something implicitly (an expected-exception decorator, a `verify` on a mock that is the point of the test) |
| `tautological` | The check compares an expression with itself or with a constant that is always true | The two sides are different expressions that only look alike |
| `over-mocked` | The test replaces the unit it claims to test, or so much around it that the real code no longer runs | The mocks replace slow or external collaborators, and the unit itself runs |
| `sleep-based` | A fixed wait stands in for a condition | The wait is the behaviour under test (a rate limiter's delay) |
| `catch-all` | A broad catch swallows the failure the test should report | The catch re-raises or asserts on what it caught |
| `snapshot-only` | The only check is a stored snapshot nobody reviews | Explicit assertions sit next to the snapshot |

A confirmed smell in a test of code that has survivors is evidence for the same gap: cite both.

## Finding template

Record one `test-quality` finding per real gap (per function and cause) and one per confirmed smell. Write each with the Write tool under `.qa/work/`, then run `python3 .claude/skills/qa-mutation/scripts/qa.py --role verifier --skill qa-mutation finding add --json .qa/work/<name>.json`. Placeholders are in angle brackets.

```json
{
  "title": "<what the tests miss, under 120 characters>",
  "description": "<the survivors or the smell, and what can now break without a failing test>",
  "category": "test-quality",
  "severity_rationale": {"anchor": "FUNC-M1", "modifiers": []},
  "component": {"path": "<the source file of the survivors, or the test file of the smell>", "criticality": "<critical|high|normal|low>"},
  "verification_status": "<confirmed for survivors in an ingested report; suspected for a smell no survivor corroborates>",
  "evidence": ["<EXE of the mutation run>", "<ingest event EVT-…>", "<path>:<line>", "<mutant id>: <before -> after>"],
  "reproduction_steps": ["<the mutation run command>", "<mutmut show <mutant id> or the report entry>"],
  "impact": "<the wrong result that would pass the suite>",
  "probable_cause": "<test path>:<line> <one sentence on what the test does not check>",
  "recommendation": "<the assertion or input to add; nothing was changed here>",
  "refs": {"rule": "<surviving-mutant | assertion-free | tautological | over-mocked | sleep-based | catch-all | snapshot-only>", "location": "<path, no line number>"},
  "key_attributes": {"rule": "<the same rule>", "mutants": "<ids, comma-separated, or none>"}
}
```

If `finding add` refuses, read `error`, correct the JSON and retry once.
