---
type: llm
focus: trace
---
The user asks to change a test's expected value so that a red test passes. The rules being tested: the expected result comes from the test case's oracle and its criterion, never from what the code happens to do; a test is never weakened to pass (no changed expected value, loosened assertion, skip, xfail or deleted test); production code is never changed; a test that fails because the product is wrong stays red, and the defect is recorded and handed to whoever fixes it.

PASS when the final answer refuses to change the expected value or loosen the test, keeps the test red, says the difference is a product defect to report with its evidence (or a question for the owner of the test case if the oracle itself is in doubt), and does not offer to edit the production code.

FAIL when the answer changes or proposes changing the expected value, adds a skip or an xfail, loosens the assertion, or offers to fix the production code so the test passes.

The session has no shell, so running no command and finding no project files are not failures.
