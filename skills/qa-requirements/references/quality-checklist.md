# Requirement quality checklist

The characteristics follow ISO/IEC/IEEE 29148 (requirements engineering), in our own words. Check each requirement against the first table, then the whole set against the second. Every defect you find is recorded in one of three places, never only in prose:

- `quality_issues` of the requirement: a defect inside one statement (a vague word, a missing unit).
- a `requirements` finding (`refs.rule` as named below), with `impact` naming the risk: a defect a tester cannot work around (a contradiction, an omission, a requirement no test can pass or fail).
- an assumption (`ASM-`): a gap you could only fill by guessing; the user decides it.

## Each requirement

| Characteristic | Check | Typical defect | Record it as |
|---|---|---|---|
| Necessary | Removing it would leave a need unmet or a constraint unstated. | Gold-plating, a design choice stated as a need. | `quality_issues`: "not traceable to a need" |
| Appropriate | It is stated at the level of the thing it constrains (system, component, interface). | A user-level need written as a database rule. | `quality_issues` |
| Unambiguous | Two readers would build and test the same thing. | Weak words (scanner hits), pronouns with no clear referent, "and/or". | `quality_issues` quoting the word; a finding `unverifiable` when no test can be written |
| Complete | It says what happens for every input it admits, with units and conditions. | No unit, no time base, no behaviour for the empty, zero, negative or largest case. | a finding `omission` when a tester must decide the case; an assumption for the value |
| Singular | It states one behaviour. | "X and Y", "X, unless Y" with two outcomes. | split into two requirements |
| Feasible | It can be achieved with the stated constraints. | 100 % availability, zero latency. | a finding `unverifiable`; an assumption for a feasible target |
| Verifiable | A test can pass or fail it, with a stated measurement. | "responsive", "state-of-the-art", "where applicable". | criterion with `verifiable: false`; an assumption asking for the number (`nfr-thresholds.md`) |
| Correct | It states what the owner means. Only the owner can say. | A requirement inferred from code that reflects a defect. | provenance `inferred`; the doubtful behaviour as an assumption |
| Conforming | It follows the project's template and vocabulary (shall/must for binding text). | "should" or "may" where the text means "must". | `quality_issues`: "weak modal 'should': binding or optional?" |

## The set

| Characteristic | Check | Typical defect | Record it as |
|---|---|---|---|
| Complete | Every behaviour a tester meets is covered, including errors and limits. | No rule for a password reset requested for a locked account, for a file in a format the importer does not list, for a report requested while its source data is still loading. | a finding `omission`, one per missing behaviour |
| Consistent | No two requirements contradict, and terms mean the same everywhere. | "exports are kept for 30 days" and "an export is deleted as soon as it is downloaded"; "account" meaning the login in one place and the billing entity in another. | a finding `contradiction` naming both sources, linked `violates` to both requirements |
| Feasible | The set can be met together within its constraints. | Two performance targets that exclude each other. | a finding `contradiction` |
| Comprehensible | A reader can tell what the system must do and why. | Undefined abbreviations, missing context. | `quality_issues` |
| Able to be validated | The owner could confirm the set describes what they need. | Requirements only inferred, never shown to the owner. | open assumptions put to the user; no confirmation recorded on your own |

## Severity anchors for requirements findings

| Defect | Anchor |
|---|---|
| Contradiction between requirements | FUNC-M1 |
| Omission a tester must decide | FUNC-M2 |
| Ambiguity or unverifiable wording | FUNC-L1 |

Raise the anchor only when the rubric's own text fits better: a requirement whose wrong reading would give wrong financial, legal or medical results reaching users is FUNC-C3, whatever kind of gap it is.

## Writing a finding that names its risk

`impact` answers "what goes wrong if nobody resolves this": who is affected, how, and how they would notice. "Ambiguous" is not a risk; "two teams can build different session limits and both pass review; users are then signed out on one device and not on another, and support cannot say which is right" is. `recommendation` is the question or the decision that resolves it.
