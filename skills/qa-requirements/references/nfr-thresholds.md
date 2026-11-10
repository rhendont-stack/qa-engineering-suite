# Non-functional criteria: measurable shapes

A non-functional requirement becomes testable when its criterion names a measure, a value, the conditions it holds under, and how it is measured. The shapes below give the measure and the conditions. **The value never comes from this file.** It comes from the source or from the user; until one of them gives it, the criterion is recorded with `verifiable: false`, and an assumption asks the question in the right-hand column.

The `type` of the requirement is the ISO/IEC 25010:2023 characteristic, spelled as the record schema spells it. The scanner's word lists are wider than the examples here; any of its hits that touches a quality attribute maps to one of these rows.

| Kind of wording | `type` | Measurable shape (`threshold`) | Question for the user (the assumption) |
|---|---|---|---|
| speed: fast, responsive, lento | performance-efficiency | p95 (and p99) of `<operation>` ≤ `<N>` ms at `<R>` requests/s, `<data size>`, measured at `<where>` | What is the slowest acceptable time for `<operation>`, at which percentile, under what load? |
| capacity: handles many users, scalable | performance-efficiency | `<N>` concurrent users / `<R>` requests/s for `<D>` minutes with error rate ≤ `<E>` % and p95 ≤ `<T>` ms | What peak load must it carry, and for how long? |
| resource use: lightweight, low overhead | performance-efficiency | CPU ≤ `<C>` %, memory ≤ `<M>` MB, or cost ≤ `<X>` per call, on `<hardware>` | Which resource matters, and what is the budget? |
| availability: reliable, robust, always on | reliability | availability ≥ `<A>` % per `<period>` excluding `<maintenance>`; MTTR ≤ `<T>`; no loss of `<records>` on `<failure>` | What downtime per month is acceptable, and what must survive which failure? |
| recovery: recovers gracefully | reliability | after `<fault>`, service resumes within `<T>` with no lost `<records>` | Which faults, and how fast must it recover? |
| ease of use: user-friendly, sencillo, fácil de usar | interaction-capability | `<P>` % of `<representative users>` complete `<task>` unaided within `<T>` minutes or `<S>` steps; or WCAG 2.2 level `<AA>` for `<pages>` | Which task, which users, and what success rate counts? |
| presentation: readable, clear output | interaction-capability | `<document or screen>` shows fields `<list>` in `<order>`, with `<format>` (units, decimals, locale) | Which fields must appear, and in what format? |
| protection: secure, safe | security | a named control: `<authentication method>`, `<authorization rule>`, `<data>` encrypted with `<algorithm>` at rest or in transit, lockout after `<N>` failures | Which assets, against which threats, with which controls? |
| reach: works everywhere, compatible | compatibility | supported `<browsers / OS / API versions>` listed by version; `<integration>` contract `<version>` | Which platforms and versions must be supported? |
| change: maintainable, modular | maintainability | a checkable proxy agreed with the team (a module boundary rule, a change-impact limit) | What change must be cheap, and how will that be checked? |
| escape clause: where applicable, if possible, según corresponda | the requirement's own type | the condition made explicit: "when `<condition>`, then `<behaviour>`; otherwise `<behaviour>`" | Under exactly which condition does this apply, and what happens otherwise? |
| precision: a calculated value with no stated rule | functional | `<rounding mode>` to `<N>` decimals (or significant digits), applied at `<step>` | Which rule, to what precision, at which step? |
| open list: including but not limited to, y así sucesivamente | the requirement's own type | an explicit, closed list | What is the complete list? |

## Recording the result

- With a value from the source: `threshold` holds the full shape with the value, and `verifiable: true`.
- Without one: `verifiable: false`, `then` says "within the limit set by ASM-nnn", and the assumption's `statement` says what is missing (not a guessed value) and its `impact` names the requirements and criteria that wait for it.
- A value the user gives in the conversation is the source for that criterion: record it, and quote their words in the answer.
