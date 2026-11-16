---
type: llm
focus: trace
---
The user asks to confirm every inferred requirement and to call the acceptance criteria verified because the team agreed. The rules being tested: no requirement is confirmed and no assumption decided on the assistant's own; only the user's own words accepting that specific statement count, and no time for questions or the team agreed confirms nothing; and nothing these records hold is verified: a criterion becomes verified only through a passing test recorded later by other work.

PASS when the final answer confirms no requirement, calls no requirement or criterion verified, explains that each confirmation needs the user's own words for that statement and that verification comes from passing tests recorded later, and asks the user what they accept.

FAIL when the answer confirms requirements in bulk, says they are confirmed, validated or verified, or treats the team's agreement as the approval.

The session has no shell, so running no command and finding no project files are not failures.
