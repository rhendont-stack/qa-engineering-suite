# Oracles

An oracle is where a test's expected result comes from. `qa-test-strategy` records one per test case (`oracle` in the TC record); this skill writes the assertion that oracle gives. The implementation is never the source of an expected value, except under `characterization`, which is labelled as such.

## The five oracles

| Oracle | The expected value comes from | Assertion shape | Counts as verifying a requirement |
|---|---|---|---|
| `requirement` | The words of the criterion the case links to, or the requirement it cites. | One input, one exact expected value or outcome taken from those words. | Yes |
| `specification` | A written contract the case names: an API schema, a protocol, a file-format standard, a documented error code. | The response or output conforms to the named part of the contract. | Yes |
| `property` | A rule that holds for every input of a class: round-trip, idempotence, an invariant, an ordering. | Many generated or listed inputs, one property asserted for each. | Yes |
| `differential` | A second implementation the case names: a reference library, the previous version, a slower exact algorithm. | The code under test and the reference give the same answer for the same inputs. | Yes |
| `characterization` | What the code does today, captured before a change. | The current output, pinned. | No: it guards against change, it does not say the behaviour is right. |

## Deriving a `requirement` value

1. Read the criterion's given, when and then. The `then` is the expected outcome; the `given` and `when` are the input and the call.
2. Use the values the criterion states. A value the criterion implies (a rule stated as a range, a formula, a set of allowed states) is computed from the rule by hand, in the test's own words, never by calling the code under test.
3. Write the arithmetic or the reasoning next to the assertion when it is not obvious, as a short comment.
4. If the criterion leaves the value open (no number, two readings, a missing rule), stop: the case goes back to `qa-test-strategy` or the user. Do not read the value off the implementation.

## Characterization

A characterization test pins current behaviour, typically before a refactor. Name it so ("pins the current ..."), keep the case's oracle `characterization`, and never report it as verifying a criterion. A case recorded with another oracle is not turned into a characterization test because the implementation disagrees with the criterion: that disagreement is a defect to report.

## When the test and the code disagree

The test was written from the oracle, so a failed assertion on the current commit is a finding about the product, not about the test. Keep the assertion, keep the run as the red check, and record the finding. Re-read the criterion once: if you misread it, correct the test from the criterion (never from the code) and say so in the answer.
