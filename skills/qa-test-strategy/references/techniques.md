# Test design techniques

The techniques of ISO/IEC/IEEE 29119-4 (2021 edition), grouped as the standard groups them, plus the experience-based
practices the suite also uses. Each has one line on when it is the right choice; that line is the shape of a test case's
`technique_reason`, which must say why the technique fits this case's rule, not restate the technique's name.

Pick the technique from the shape of the rule and the risk, not from habit. A test case may combine two (a decision table whose
conditions are then probed at their boundaries); record the one that drives the case's values in `technique` and mention the
other in the reason.

## Specification-based

| Technique | Choose it when | Example (not from any project here) |
|---|---|---|
| Equivalence partitioning | An input or output splits into classes the rule treats alike; one value per class is enough. | A password of 12 to 64 characters: classes too short, valid, too long. |
| Classification tree | Several inputs each split into classes and you need to see and choose the combinations. | A search filter: format x language x availability, drawn as a tree before picking rows. |
| Boundary value analysis | A rule states a limit; failures cluster on the limit and its neighbours. Use each limit and the values just inside and outside it. | 11, 12, 64 and 65 characters for the password above. |
| Syntax testing | The input has a grammar (a file format, a query language, an identifier pattern). | Valid and malformed ISBNs: wrong length, wrong check digit, stray separators. |
| Combinatorial (all combinations, pairwise, each choice, base choice) | Many independent parameters, too many combinations to test all; pairwise catches the faults two parameters cause together. | Device x operating system x screen reader for a sign-up form. |
| Decision table | The outcome depends on a combination of conditions; every column is a rule a tester can check. | Renewal allowed? member in good standing x item reserved x renewals left. |
| Cause-effect graphing | Like a decision table, when causes combine through and/or/not and you want the minimal set of columns. | Alarm raised when (door open and system armed) or tamper detected. |
| State transition | The behaviour depends on history: states, events and guarded transitions; test each transition and the invalid ones. | A door lock: locked, unlocked, alarmed; a wrong code entered repeatedly from locked. |
| Scenario testing (use case) | A user goal crosses several steps or components; test the main path and each alternative and exception path. | Reserve an item, get notified, collect it, return it late. |
| Random testing | The input space is large and an oracle can judge any input (a property, a reference implementation). | Random strings fed to a parser and checked against a reference parser. |
| Metamorphic testing | There is no exact expected output, but a relation between outputs must hold. | A search for "river" returns at least the results of "river boat". |
| Requirements-based testing | Each requirement or criterion is checked directly with the values it states; the default for a criterion with a concrete expected result. | "A renewal adds 14 days to the due date": one case per stated outcome. |

## Structure-based

| Technique | Choose it when | Example |
|---|---|---|
| Statement testing | You need evidence that every statement of a critical unit ran at least once. | Every line of a permission check. |
| Branch / decision testing | Each outcome of each decision must be taken; the usual floor for critical code. | Both outcomes of every `if` in a password reset rule. |
| Branch condition, branch condition combination, MC/DC | A decision has several conditions and each must be shown to affect the outcome on its own (MC/DC for safety-critical logic). | `if enrolled and not suspended and verified` with each condition flipped alone. |
| Data flow testing | A value is defined in one place and used in another, and a wrong path between them is the risk. | A setting read at start-up and used after a reload. |

Structure-based cases come from reading the code; their expected results still come from the requirement (`oracle:
requirement` or `specification`), never from what the code happens to return.

## Experience-based

| Technique | Choose it when | Example |
|---|---|---|
| Error guessing | Experience says code of this kind breaks on certain inputs, and the requirements do not list them. | Missing or blank fields, the largest allowed size, duplicate submissions, events out of order. |
| Exploratory testing (a practice, not a 29119-4 technique) | The risk is not well understood yet; a time-boxed charter learns where to look. Record the charter as the test case. | "Explore renewals of reserved items for 45 minutes to find states the rules do not cover." |
| Property-based testing (a way to automate random testing) | A property must hold for every input in a class; a tool generates the inputs and shrinks failures. | Encoding then decoding returns the original text, for any text. |

## Writing the reason

`technique_reason` is one line that names the feature of this rule that makes the technique fit:

- Weak: "Boundary value analysis is a standard technique." (true of any case)
- Weak: "boundary value analysis" (the technique's name again)
- Good: "The rule allows 12 to 64 characters; faults sit at 11/12 and 64/65."
- Good: "Several conditions decide a renewal together; a decision table gives one case per combination the rule distinguishes."

## Levels

Choose the level from the architecture: the lowest level at which the failure can be observed with a trustworthy oracle. A rule
inside one function is a unit case; a rule that only exists when two components meet (a message one writes and the other
reads) is an integration case; a user goal across the system is an end-to-end scenario. Do not fix a pyramid ratio in
advance.

## Oracles

| `oracle` | Use when |
|---|---|
| `requirement` | A recorded criterion states the expected result. |
| `specification` | A document other than the requirements states it (an API description, a standard, a file format). |
| `property` | The case checks a property over many inputs (property-based, metamorphic). |
| `differential` | The result is compared with another implementation (a reference, the previous version). |
| `characterization` | No source states the result and the case only pins what the code does now. It never counts as verifying a requirement; say so and ask for the rule. |
