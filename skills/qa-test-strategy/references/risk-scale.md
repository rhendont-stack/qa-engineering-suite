# Risk scale

Risk-based testing in the ISTQB sense: a product risk is something that can go wrong in what the project delivers, its level is
likelihood x impact, and the level decides how much testing the risk gets and in what order. The kernel records both numbers on
a 1-5 scale (`scale: "1-5"`); this file fixes what each number means, so two people rating the same risk land on the same score.

Rate from evidence you can cite (a requirement line, a code path, a recorded finding), not from a feeling. When the evidence is
thin, say so in the risk's title or in the answer; do not pad the number to make it look certain.

## Likelihood: how probable is the failure in this code, as it stands?

| Value | Label | Use when |
|---|---|---|
| 1 | Rare | Simple, stable code on a well-specified rule, already exercised by passing tests. |
| 2 | Unlikely | Simple code and a clear rule, but no test exercises it yet. |
| 3 | Possible | Some of: branching logic, a rule with a stated limit, code that changed recently, no tests. |
| 4 | Likely | Several of the above, or the rule itself is ambiguous, contradictory or incomplete, so two people would build it differently. |
| 5 | Almost certain | You can already point at code that does not match the rule, or a recorded finding says the rule cannot be built as written. |

Cues that raise likelihood: a stated limit, input from outside the system, concurrency, new or heavily changed code, code no
test exercises, and a requirement with an open finding or assumption.

## Impact: if it fails in use, how bad is it?

| Value | Label | Use when |
|---|---|---|
| 1 | Negligible | Cosmetic; nobody is misled and nothing is lost. |
| 2 | Minor | A visible fault with a simple workaround; no data or financial effect. |
| 3 | Moderate | A feature gives a wrong result some users will notice; a workaround exists. |
| 4 | Major | A core feature gives a wrong result or is unavailable; no reasonable workaround; many users affected. |
| 5 | Severe | Financial loss to users or to the business, data loss or corruption, a security or privacy breach, legal or safety harm. |

Rate impact on the worst credible outcome, not the average one, and name that outcome in the risk's title.

## Level, band and what each band gets

Score = likelihood x impact (1-25).

| Band | Score | Rule | Coverage it gets |
|---|---|---|---|
| Critical | 15-25, or any impact 5 | An impact-5 risk is critical whatever its likelihood: likelihood is the least reliable estimate. | At least one test case aimed at that exact failure, priority P0, `criticality: critical`, designed with the technique that targets it (a limit gets boundary value analysis). No accepted gap unless the user accepts it in their own words. |
| High | 10-14 | | At least one test case, priority P1. |
| Medium | 5-9 | | Covered by the test cases of the criteria it touches, priority P2. |
| Low | 1-4 | | Priority P3, or an accepted gap the user gives. |

A test case that mitigates a critical risk is `criticality: critical`: the release gate's `no_blocked_critical` criterion counts
those, so a critical test case that was never run blocks a release.

## Accepted gaps

A gap is a risk the team decides not to test. Only a person can accept it: `accepted_gap.accepted_by` names who accepted it in
the conversation, and `accepted_gap.reason` quotes or closely restates their words. Never fill either in for the user, never
write "user", "team" or "product owner" because it seems likely they would agree, and never accept a gap to get a risk recorded.

## Worked rating

A library service lets a member renew a loan no more than four times, and never when the item is reserved by someone else.

| Risk | L | I | Score | Band | Why |
|---|---|---|---|---|---|
| A reserved item is renewed, so the next member never gets it | 3 | 3 | 9 | Medium | Two rules act on one decision; a wrong result is noticed and fixable by staff. |
| A member's loan history is shown to another member | 2 | 5 | 10 | Critical | Privacy breach: impact 5 makes it critical even at likelihood 2. |
| The renewal count resets when an item is returned and borrowed again | 4 | 3 | 12 | High | The source does not say whether the count belongs to the loan or to the member. |
