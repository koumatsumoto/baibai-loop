---
ticker: "XXXX"
decision_event_id: decision-YYYYMMDD-XXXX-review-target
research_ref: records/05-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
trade_ref: records/06-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
playbook_id: valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth
classification: success | failure | invalidated | inconclusive
verified_at: "YYYY-MM-DDTHH:MM:SS+09:00"
outcome:
  horizon_bd: 15
  start_price_basis: first_fill_vwap_yen | research_max_entry_price_yen | candidate_run_close_adjusted_close
  start_price_yen: 0
  end_price_yen: 0
  gross_return_pct: 0.0
  market_baseline_return_pct: 0.0
  sector_baseline_return_pct: 0.0
  market_relative_return_pct: 0.0
  sector_relative_return_pct: 0.0
  primary_relative_baseline: sector
  primary_relative_return_pct: 0.0
  execution_costs_yen: 0
  net_return_pct: 0.0
attribution_targets:
  - type: evidence_hit | macro_context | sizing | execution | playbook
    target_id: "id"
    effect: helped | hurt | neutral | unknown
    confidence: high | medium | low
structured_field_provenance:
  outcome: machine_calculated
  attribution_targets: analyst_written | llm_drafted_analyst_confirmed
---

# Review: YYYY-MM-DD XXXX [銘柄名]

**成分**: Decision lifecycle の **reviews / attribution**（[`/docs/components/reviews.md`](/docs/components/reviews.md)）

**Trade**: [records/06-trades/YYYY/MM/YYYY-MM-DD-XXXX.md](/records/06-trades/YYYY/MM/YYYY-MM-DD-XXXX.md)
**Research**: [records/05-research/YYYY/MM/YYYY-MM-DD-XXXX-*.md](/records/05-research/YYYY/MM/YYYY-MM-DD-XXXX-*.md)

## Outcome

- Playbook: [valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth]
- Decision event: `decision_event_id`
- Review horizon: 15 / 30 business days
- Return basis: market / sector relative return
- Execution costs: commission / tax / slippage

### Price evidence

| Field | Value |
| --- | --- |
| source_url | |
| fetched_at | YYYY-MM-DDTHH:MM:SS+09:00 |
| start_date | YYYY-MM-DD |
| evaluation_date | YYYY-MM-DD |
| price_basis | close_unadjusted / adjusted_close / intraday_last |
| benchmark_source_url | |
| same_basis_note | |

## Hypothesis check

- **原 thesis**（research から）: [1-2 段落転写]
- **実際に起きたこと**: [事実として記述]
- **Thesis の的中度**: [完全的中 / 部分的中 / 外れ]

## Process check

- **Classification**: success / failure / invalidated / inconclusive
- **Primary attribution**: evidence_hit / macro_context / sizing / execution / playbook
- **Confidence**: high / medium / low
- **Execution / sizing / macro context の確認**: [entry preflight、注文、exit timing の妥当性]

## Lessons

[判断改善に使う要点。evidence / macro / sizing / execution / playbook のどれに帰属するかを明確にする]

## Next actions

- 月次 retro ([`retro-YYYYMM.md`](/records/07-reviews/YYYY/retro-YYYYMM.md)) へ渡す要点:
- Playbook 改訂提案（あれば）:
- Follow-up issue（あれば）:

---

参照: [`/docs/components/reviews.md`](/docs/components/reviews.md), [`/docs/screening/failure-taxonomy.md`](/docs/screening/failure-taxonomy.md)
