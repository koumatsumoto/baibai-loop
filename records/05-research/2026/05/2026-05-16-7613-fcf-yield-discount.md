---
ticker: '7613'
name: シークス
playbook_id: fcf-yield-discount
playbook_ref:
  ref_path: records/_playbooks/fcf-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
policy_ref:
  ref_path: records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md
  effective_from: '2026-05-01T00:00:00+09:00'
policy_applicability: active
calendar_refs:
  business_days:
    ref_path: records/_calendars/business-days/2026-05.yaml
  events:
    ref_path: records/_calendars/events/2026-05.yaml
  corporate_actions:
    ref_path: records/_calendars/corporate-actions/2026-05.yaml
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-08-7613-cashflow-yield-discount
- source: candidate
  evidence_hit_id: candidate-2026-05-08-7613-fcf-yield-discount
research_decision:
  outcome: rejected
  posture: dropped
  rejection_reason: lower_priority_after_q1_profit_and_cf_check
  reason_code: q1_fcf_durability_not_confirmed
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-08.yaml
  screen_run_id: screening-20260508
  ticker: '7613'
  candidate_id: candidate-2026-05-08-7613
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
- records/02-brief/2026/05/2026-05-10-world-weekly-us-jobs-nikkei-wti.yaml
- records/02-brief/2026/05/2026-05-13-world-daily-us-cpi-boj-opinions.yaml
ai_draft: true
published_at: '2026-05-16T10:40:00+09:00'
recorded_at: '2026-05-16T10:40:00+09:00'
tradable_at: null
macro_regime_gate:
  aggregate_status: neutral
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs:
  - scope: sector
    key: 卸売業
    status: neutral
    source_ref: records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml
    weight_or_materiality: medium
    confidence: low
policy_overrides: []
external_refs:
- ref_path: records/_external/siix/2026-05-16-fy2026-q1-official-ir.md
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-08-7613-cashflow-yield-discount
  effective_sizing_eligible: false
  evaluated_at: '2026-05-16T10:40:00+09:00'
  reason_code: q1_cash_flow_statement_unavailable
- evidence_hit_id: candidate-2026-05-08-7613-fcf-yield-discount
  effective_sizing_eligible: false
  evaluated_at: '2026-05-16T10:40:00+09:00'
  reason_code: q1_profit_quality_not_enough
research_evidence_hits:
- evidence_hit_id: research-7613-fy2026-q1-net-debt-improved
  decision_role: freshness_adjustment
  evidence_polarity: neutral
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/siix/2026-05-16-fy2026-q1-official-ir.md
  recorded_at: '2026-05-16T10:40:00+09:00'
- evidence_hit_id: research-7613-fy2026-q1-op-decline-and-cf-gap
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/siix/2026-05-16-fy2026-q1-official-ir.md
  recorded_at: '2026-05-16T10:40:00+09:00'
independent_evidence_count: 0
raw_playbook_concurrence_count: 2
sizing_eligible_playbook_concurrence_count: 0
raw_evidence_family_count: 1
sizing_eligible_evidence_family_count: 0
conviction_tier: low
conviction_tier_path: count_breadth
depth_verification_ref: null
position_sizing_overlay:
  paper_proxy_position_size_oku: 0.0
  paper_proxy_position_size_yen: 0
  real_order_intent_yen: 0
  adv_participation_pct: 0.0
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual:
  if_approved:
    hypothetical_paper_proxy_position_size_oku: 0.01
    hypothetical_paper_proxy_position_size_yen: 1000000
thesis_payoff:
  max_entry_price_yen: 1500
  target_price_yen: 1800
  stop_loss_yen: 1350
  time_horizon_bd: 40
  invalidation_conditions:
  - Q1 operating profit decline proves the FCF yield screen is backward-looking.
  - Q1 cash-flow statement remains unavailable and FCF durability cannot be verified.
  - FX-adjusted sales remains negative.
  entry_trigger: rejected_after_q1
  expected_upside_pct: 20.0
  expected_downside_pct: 10.0
  risk_reward_ratio: 2.0
tracking:
  mode: none
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 662
sector_33: 卸売業
avg_turnover_oku: 3.2
valuation:
  per_forward: null
  per_trailing: 24.86
  pbr: 0.6
  ev_ebitda: 4.5
  p_s: 0.23
  pcfr: 2.5
  ocf_yield: 0.401
  fcf_yield: 0.35
  net_cash_to_market_cap: -0.2218
  cash_to_market_cap: 0.4502
  price_to_equity: 0.6376
  equity_ratio: 0.4993
  primary_metric:
  - fcf_yield
  - ocf_yield
---

# Research: 2026-05-16 7613 シークス fcf-yield-discount

## Thesis

7613 は今回の Q1 後確認では追加投入候補から外す。2026-05-08 candidates の FCF yield 35.0%、OCF yield 40.1%、PBR 0.60、P/S 0.23 は割安に見えるが、2026-05-13 Q1 は売上高 +2.0%、営業利益 -5.4%。決算説明資料の為替影響除きでは売上高 -2.9%、営業利益 -6.1% で、営業面の底打ちが弱い。

net debt と棚卸資産は悪化していない。そこは継続 watch に値するが、Q1 CF 計算書がなく FCF durability を確認できず、6835 のような利益進捗と net cash の組み合わせもない。6310 より debt risk は軽いが、営業利益減益と FX-adjusted 減収を見て、今回の追加投入順位からは落とす。

## Macro regime gate

- 判定: neutral / pass。
- Sector: 卸売業。
- Source: `outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml`。
- 注意: 卸売業は商社 mix が広く、sector 一括 supportive ではない。macro は採用を押し上げない。

## FCF snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| FCF TTM | 23,163 百万円 | 2026-05-08 candidate source |
| FCF yield | 35.0% | 数値上は強い |
| OCF yield | 40.1% | 数値上は強い |
| Q1 売上高 | 74,036 百万円 | +2.0%、為替影響除き -2.9% |
| Q1 営業利益 | 2,687 百万円 | -5.4%、為替影響除き -6.1% |
| Q1 CF | 非開示 | FCF durability 未確認 |
| 概算 net debt | 5,650 百万円 | FY2025 末候補値より改善方向 |

FCF / OCF yield は backward-looking な候補 evidence として残るが、Q1 の営業利益減少と CF 非開示で sizing eligible にはしない。

## Capex quality

候補 snapshot の capex tag は `purchase_of_fixed_assets` で exact。Q1 では CF 計算書が作成されていないため、capex normalisation を新しい一次情報で確認できない。

## Working capital quality

棚卸資産合計は 56,564 百万円で、2025-12 末 56,490 百万円から大きくは増えていない。売上債権は 60,515 百万円へ増加した。working capital は大きく崩れていないが、営業 CF の裏付けがないため採用根拠にしない。

## Earnings quality

親会社株主帰属四半期純利益は +20.3% だが、営業利益は -5.4%。為替影響を除く売上と営業利益も減少しており、P/S や PBR の低さを事業回復で説明するには弱い。

## Shareholder return

配当予想や還元は補助材料に留まる。今回の判断では、営業利益減益と Q1 CF 非開示を上回る採用根拠にはしない。

## Entry

新規 entry はしない。今回の #122 対応では 6835 を優先し、7613 は次回 screening で再選定されるまで追加投入候補にしない。

## Exit

未保有のため exit なし。

## Invalidation

- Q1 営業利益が -5.4%。
- 為替影響除き売上高が -2.9%。
- Q1 CF 非開示で FCF durability を確認できない。

## Position size

research_decision は rejected。paper proxy、real order intent、ADV participation はすべて 0。追加投入候補化はしない。
