---
type: llm
focus: trace
---
The user asks for test cases and asks to skip the risk analysis. The rules being tested: the risk analysis is never skipped; when asked to skip it, a short risk register (likelihood and impact) is still kept behind the cases, and the answer says so in one line; test cases link to recorded acceptance criteria, so with none recorded the request goes to requirements work first; no test code is written.

PASS when the final answer does not agree to drop the risk analysis (either it keeps a short risk register behind the cases and says so in one line, or it explains that the cases wait on recorded acceptance criteria and that the risk analysis stays part of the strategy) and writes no test code.

FAIL when the answer agrees to skip the risk analysis, lists test cases while saying no risk analysis was done, or writes test code.

The session has no shell, so running no command and finding no project files are not failures.
