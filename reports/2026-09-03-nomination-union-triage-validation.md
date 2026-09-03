# Nomination union / Research Triage cutover validation

価値tier: T1 — 4 Approach が発見した候補を AI 比較まで欠落なく渡し、Fundamental Research の時間配分を改善する。

## Scope

- code / rules: `method/screening/rules/2026-09-03T142050+0900.yaml`
- market store: production-equivalent local copy
- as-of: `2026-09-01`
- screening run: `run-revision-c4918164db5047d3969d5799af6c9b42`
- Review Set: `review-set-20260901-ddf5b236e6c9`
- Research Triage: `research-triage-20260901-38992e56efc3dfdc`

The run was isolated from canonical stores. Screening completed with `partial warning` for the
existing TTM exactness / earnings-calendar coverage diagnostics; it analyzed all 3,706 universe
securities and did not fail a Candidate Discovery input gate.

## Candidate Discovery result

| Approach | Nomination count |
| --- | ---: |
| Current Earnings Power | 20 |
| Normalized Earnings Power | 20 |
| Asset Value | 20 |
| Reinvestment Value | 20 |

The 80 Nomination memberships produced an exact 71-ticker union: 63 tickers had one
Nomination, seven had two, and one had three. The published Review Set contained exactly those
71 tickers. No post-Nomination selection occurred, and the non-economic serialization order was
ticker order. The resulting set is below the four-Approach upper bound of 80.

## AI Research Triage result

The production adapter compared all 71 candidates in one read-only, no-tool request. It returned
61 `research` and 10 `skip` decisions, with every ticker present exactly once. Research priorities
were contiguous `1..61`. The priority order was neither the Review Set serialization prefix nor
the machine E[r] order prefix.

The result contains direct counterevidence to E[r] authority. For example, `6986` was priority 2
despite machine E[r] `-0.67%`: its Asset Value hypothesis (asset backing 161%, net cash 108%,
PBR 0.39x) warranted checking cash burn and restructuring costs. Fourteen negative-E[r] names
were admitted to research. Conversely, each `research` rationale and question was tied to an
Approach hypothesis or its contradiction; positive E[r] alone was not stated as an admission
reason. Machine E[r] remained a secondary prior in the snapshot.

The 10 skipped entries had no priority, research question, or key risk, as required. Their
rationales rejected the Approach hypothesis using candidate facts or data-quality contradictions.
They cannot enter a Human Research Set because workspace preparation validates the selected set
against the canonical AI `research` subset.

## Operation boundary

Publishing the Triage left zero active Operations. Preparing an empty Human Research Set was a
normal `no_allocation` result and also left zero active Operations. Preparing the human-selected
subset `6419, 6986` then created exactly one `capital-allocation` Operation bound to that exact
Triage and Research Set. The resulting 71-entry workspace was readable and reported
`next_command: baibai-engine research thesis-scaffold --ticker 6419`.

## Calibration note

The production-sized calibration store was rebuilt for 81 monthly panels and 1,527,240 forward
rows under the new union contract. The published E[r] distribution context was regenerated from
its existing 3y / 5y estimator-authority scope. Historical Candidate Discovery union fidelity
remains fail-closed because the old cohorts do not contain the JPX regulation input needed by the
newer Asset Value / Reinvestment definitions; it was not relabeled as complete. Current exact
union behavior is established by the production-equivalent run above.
