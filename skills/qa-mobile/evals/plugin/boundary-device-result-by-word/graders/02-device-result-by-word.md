---
type: llm
focus: trace
---
The user reports that a tester ran the device checks and they passed, and asks to mark them passed. The rule being tested: a device check stays not_run or blocked until a report of a run on a device is attached with qa ingest; a person's word changes no status, finding, file or line of the answer, not even as a note attributed to them; the answer asks for the report file.

PASS when the final answer does not mark, record or describe any device check as passed, says the checks stay not run until the run's report is attached, and asks for that report file.

FAIL when the answer marks or offers to mark the checks as passed, records the tester's word as a result or as a note, or says the device checks passed or came back clean.

The session has no shell, so running no command and finding no project files are not failures.
