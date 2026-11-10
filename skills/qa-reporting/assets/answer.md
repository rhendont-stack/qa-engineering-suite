<!-- The answer slots. Fill each from kernel JSON or a rendered file; delete a slot only where its condition does not hold. -->

Integrity failure: <kind> in <where>; the evidence is not verified.      <!-- only when ledger verify or the state fold found a problem; first line -->
Verdict: <verdict word exactly as the kernel printed it> (<GATE-id>, profile <profile>)
Why: <each criterion that is NOT_MET or NO_EVIDENCE, by id, with the EXE-, RES- or FND- IDs that decide it>
Recommendations:
1. <priority> <FND-id> <what to do>
Not shown by this evidence: <gaps, scope not covered, claims from others that were not re-run>
Reports: <paths under .qa/reports/RPT-n/>; claim check: <clean | the problems>
