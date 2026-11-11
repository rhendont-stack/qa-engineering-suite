<!-- The answer slots. Fill each from kernel JSON or a rendered file; delete a slot only where its condition does not hold. -->

Integrity failure: <kind> in <where>; the evidence is not verified.      <!-- only when ledger verify or the state fold found a problem; first line -->
Verdict: <verdict word exactly as the kernel printed it> (<GATE-id>, profile <profile>)
Why: <each criterion that is NOT_MET or NO_EVIDENCE, by id, with the EXE-, RES- or FND- IDs that decide it>
Traceability: requirements_verified <the outcome the gate computed>; <each REQ- with its AC- and TC- IDs>; gaps: <every gap in .qa/trace/matrix.json as kind ID, or none>      <!-- from trace matrix; say "no requirement recorded" when there is none -->
Recommendations:
1. <priority> <FND-id> <what to do>
Not shown by this evidence: <gaps, scope not covered, claims from others that were not re-run>
Reports: <paths under .qa/reports/RPT-n/>, .qa/trace/matrix.json; claim check: <clean | the problems>
