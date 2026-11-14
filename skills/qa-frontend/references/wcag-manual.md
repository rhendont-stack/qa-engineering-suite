# WCAG 2.2 A and AA: what a person checks

WCAG 2.2 has 55 success criteria at levels A and AA (4.1.1 Parsing was removed in 2.2). Conformance at level AA means every one of them is met on every page of the scope, for complete processes. No tool decides that, and neither does this skill: an automated run decides part of some criteria, and the rest is a person's review with a keyboard, a screen reader, zoom and the content in front of them.

`wcag_table.py` writes one entry per criterion. Its two kinds are below.

## Criteria an axe-core rule tests in part (21)

axe-core 4.14.0, under the tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa` and `wcag22aa`, has rules naming these: 1.1.1, 1.2.2, 1.3.1, 1.3.5, 1.4.1, 1.4.2, 1.4.3, 1.4.4, 1.4.12, 2.1.1, 2.2.1, 2.2.2, 2.4.1, 2.4.2, 2.4.4, 2.5.3, 2.5.8, 3.1.1, 3.1.2, 3.3.2, 4.1.2. Without an axe run each is `not_run` with the reason `tool-missing`. With one, a violation makes it `failed`; no violation leaves it `inconclusive`, because the rules test part of the criterion (that an image has an alt text, not that the text says what the image shows; that a control has a name, not that the name is right). A person still reviews each.

## Criteria no automated rule decides (34)

Each is `not_run` with the reason `manual-only`. The last column names the check of this skill that gives a lead, never a decision.

| Criterion | Level | Title | What a person checks | Lead from this skill |
|---|---|---|---|---|
| 1.2.1 | A | Audio-only and Video-only (Prerecorded) | A transcript or an audio track for each prerecorded audio-only or video-only item | none |
| 1.2.3 | A | Audio Description or Media Alternative (Prerecorded) | An audio description or a full text alternative for prerecorded video | none |
| 1.2.4 | AA | Captions (Live) | Captions for live audio | none |
| 1.2.5 | AA | Audio Description (Prerecorded) | An audio description for prerecorded video | none |
| 1.3.2 | A | Meaningful Sequence | The reading order a screen reader follows keeps the meaning | none |
| 1.3.3 | A | Sensory Characteristics | Instructions do not rely only on shape, colour, size, position or sound | none |
| 1.3.4 | AA | Orientation | The page works in portrait and landscape | the phone and tablet viewports, portrait only |
| 1.4.5 | AA | Images of Text | Text is text, not a picture of it, except logos | none |
| 1.4.10 | AA | Reflow | No two-dimensional scrolling at 320 CSS px wide (400% zoom) | the overflow check at 375 px; the static scan's fixed widths |
| 1.4.11 | AA | Non-text Contrast | 3:1 for control borders, focus indicators and meaningful graphics | none |
| 1.4.13 | AA | Content on Hover or Focus | Tooltips and popups can be dismissed, hovered and stay until dismissed | none |
| 2.1.2 | A | No Keyboard Trap | Focus can always leave a component with the keyboard | none |
| 2.1.4 | A | Character Key Shortcuts | Single-key shortcuts can be turned off or remapped | none |
| 2.3.1 | A | Three Flashes or Below Threshold | Nothing flashes more than three times a second | none |
| 2.4.3 | A | Focus Order | Tab order follows the meaning and operation of the page | none |
| 2.4.5 | AA | Multiple Ways | More than one way to find a page (navigation, search, site map) | none |
| 2.4.6 | AA | Headings and Labels | Headings and labels describe their topic or purpose | none |
| 2.4.7 | AA | Focus Visible | Every focused control shows where focus is | none |
| 2.4.11 | AA | Focus Not Obscured (Minimum) | A focused control is not hidden by sticky headers or overlays | none |
| 2.5.1 | A | Pointer Gestures | Multipoint or path gestures have a single-pointer alternative | none |
| 2.5.2 | A | Pointer Cancellation | Actions fire on up-event, or can be aborted or undone | none |
| 2.5.4 | A | Motion Actuation | Shake or tilt actions have a control alternative and can be turned off | none |
| 2.5.7 | AA | Dragging Movements | Dragging has a single-pointer alternative | none |
| 3.2.1 | A | On Focus | Focus alone does not change the context | none |
| 3.2.2 | A | On Input | Changing a setting does not change the context unannounced | none |
| 3.2.3 | AA | Consistent Navigation | Repeated navigation keeps its order across pages | none |
| 3.2.4 | AA | Consistent Identification | The same function has the same name across pages | none |
| 3.2.6 | A | Consistent Help | Help mechanisms appear in the same relative place | none |
| 3.3.1 | A | Error Identification | A detected input error is identified and described in text | the error-state check; the email check |
| 3.3.3 | AA | Error Suggestion | A known correction is suggested | none |
| 3.3.4 | AA | Error Prevention (Legal, Financial, Data) | Legal, financial and data submissions can be reversed, checked or confirmed | none |
| 3.3.7 | A | Redundant Entry | Information already given in a process is not asked for again | none |
| 3.3.8 | AA | Accessible Authentication (Minimum) | No cognitive function test to sign in without an alternative | none |
| 4.1.3 | AA | Status Messages | Status messages reach a screen reader without moving focus | the loading and error-state checks show the messages exist, not that they are announced |

## Answering a conformance question

- Never answer yes, and never write that the project is compliant, conformant or meets WCAG, AA or accessibility: in the answer, a finding, the table or any file. A failure you found is enough to say it does not meet a criterion; nothing this skill runs is enough to say it meets one.
- Say what was checked (the table's counts), what failed with its finding and `path:line`, what did not run and why, and name manual-only criteria the review still needs.
- For a contract: the claim is the owner's, after a full review by a person (often written up as an accessibility conformance report); the table and the findings are its input.
