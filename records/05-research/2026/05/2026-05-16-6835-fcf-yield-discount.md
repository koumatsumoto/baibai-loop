---
ticker: '6835'
name: アライドテレシスホールディングス
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
  evidence_hit_id: candidate-2026-05-08-6835-fcf-yield-discount
research_decision:
  outcome: deferred
  posture: wait_for_event
  deferral_reason: data_gap
  revisit:
    trigger: fy2026_q2_cash_flow_statement
    revisit_after: '2026-08-14'
    expires_at: '2026-08-31'
    blocking_conditions:
    - Q2 cash-flow statement must confirm FCF / OCF durability.
    - Inventories and receivables must not keep building against weak sales growth.
    - Full-year guidance downside risk must remain contained.
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-08.yaml
  screen_run_id: screening-20260508
  ticker: '6835'
  candidate_id: candidate-2026-05-08-6835
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
- records/02-brief/2026/05/2026-05-10-world-weekly-us-jobs-nikkei-wti.yaml
- records/02-brief/2026/05/2026-05-13-world-daily-us-cpi-boj-opinions.yaml
ai_draft: true
published_at: '2026-05-16T10:35:00+09:00'
recorded_at: '2026-05-16T10:35:00+09:00'
tradable_at: null
macro_regime_gate:
  aggregate_status: supportive
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs:
  - scope: sector
    key: 電気機器
    status: supportive
    source_ref: records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml
    weight_or_materiality: high
    confidence: low
policy_overrides: []
external_refs:
- ref_path: records/_external/allied-telesis/2026-05-16-fy2026-q1-official-ir.md
- ref_path: records/_external/deepresearch/2026-05-13-top3-bargain-selection.md
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-08-6835-cashflow-yield-discount
  effective_sizing_eligible: false
  evaluated_at: '2026-05-16T10:35:00+09:00'
  reason_code: q1_cash_flow_statement_unavailable
- evidence_hit_id: candidate-2026-05-08-6835-fcf-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-16T10:35:00+09:00'
  reason_code: q1_event_risk_cleared_for_small_starter
research_evidence_hits:
- evidence_hit_id: research-6835-fy2026-q1-profit-net-cash-supports
  decision_role: freshness_adjustment
  evidence_polarity: neutral
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/allied-telesis/2026-05-16-fy2026-q1-official-ir.md
  recorded_at: '2026-05-16T10:35:00+09:00'
- evidence_hit_id: research-6835-q1-cf-statement-unavailable
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/allied-telesis/2026-05-16-fy2026-q1-official-ir.md
  recorded_at: '2026-05-16T10:35:00+09:00'
independent_evidence_count: 1
raw_playbook_concurrence_count: 1
sizing_eligible_playbook_concurrence_count: 1
raw_evidence_family_count: 1
sizing_eligible_evidence_family_count: 1
conviction_tier: medium
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
  max_entry_price_yen: 270
  target_price_yen: 340
  stop_loss_yen: 230
  time_horizon_bd: 40
  invalidation_conditions:
  - Q2 cash-flow statement invalidates FCF / OCF durability.
  - Inventories and receivables continue to build while sales growth stalls.
  - Full-year guidance downside risk rises after Q1 outperformance.
  entry_trigger: q2_cf_revisit
  expected_upside_pct: 25.93
  expected_downside_pct: 14.81
  risk_reward_ratio: 1.75
tracking:
  mode: re_examination
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 282
sector_33: 電気機器
avg_turnover_oku: 1.6
valuation:
  per_forward: null
  per_trailing: 9.76
  pbr: 1.32
  ev_ebitda: 3.3
  p_s: 0.57
  pcfr: 4.2
  ocf_yield: 0.2388
  fcf_yield: 0.2202
  net_cash_to_market_cap: 0.3786
  cash_to_market_cap: 0.6028
  price_to_equity: 1.3242
  equity_ratio: 0.4378
  primary_metric:
  - fcf_yield
---

# Research: 2026-05-16 6835 アライドテレシスホールディングス fcf-yield-discount

## Thesis

6835 は Q1 後に「買ってもよい小さい候補」までは残ったが、今回は注文しない。2026-05-08 candidates の FCF yield 22.0%、net cash / market cap 37.9%、PER 9.76、EV/EBITDA 3.3 は残しつつ、2026-05-15 Q1 で売上高 +2.3%、営業利益 +19.9%、親会社株主帰属四半期純利益 +95.4% を確認したため、イベント直前リスクは通過した。

ただし、Q1 では CF 計算書が作成されていない。在庫と売上債権も増えており、通期会社計画では営業利益・純利益の減益が残る。配当利回りは 270 円前提で約 3.3% と悪くないが、リスクを下げたい局面であえて資金と注意を使うほどの非対称性はない。判断は `deferred` とし、Q2 CF 後に再確認する。

## Macro regime gate

- 判定: supportive / pass。
- Sector: 電気機器。
- Source: `outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml`。
- 注意: 電気機器 supportive は AI / 半導体関連の色が強い。6835 はネットワーク機器で直接度は低く、macro gate は pass だが conviction は medium 止まり。

## FCF snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| FCF TTM | 6,221 百万円 | 2026-05-08 candidate source |
| FCF yield | 22.0% | primary selected evidence |
| OCF yield | 23.9% | Q1 CF 非開示のため今回は sizing eligible から除外 |
| Net cash / market cap | 37.9% | balance sheet support |
| Q1 売上高 | 13,120 百万円 | +2.3% |
| Q1 営業利益 | 1,439 百万円 | +19.9% |
| Q1 親会社株主帰属四半期純利益 | 1,012 百万円 | +95.4% |

Q1 の利益進捗と net cash は候補継続を支持する。反対に、Q1 CF がないため FCF の durability は Q2 で再確認する。

## Capex quality

候補 snapshot の capex tag は `purchase_of_fixed_assets` で取れており、FCF lane は exact。Q1 では CF 計算書がなく capex の最新実績は確認できないため、Q2 まで注文しない。

## Working capital quality

現金及び預金は 17,029,080 千円から 17,294,006 千円へ増加した。一方、売上債権と商品及び製品は増えている。Q1 時点では cash balance は崩れていないが、working capital が軽いとは確認できない。

## Earnings quality

日本事業は NEXT GIGA、医療機関、自治体ネットワーク向け需要、スイッチ製品が支えた。海外は EMEA と APAC が減収で、地域分散の質はまだ強くない。通期予想は据え置きで、Q1 が良くても会社計画では通期営業利益 -22.0% が残る。

## Shareholder return

年間配当予想は 9 円で据え置き。Q1 には自己株式 553,600 株、154,452 千円の取得もある。配当と自己株式取得は net cash thesis の補助になるが、CF 非開示を埋めるほどではない。

## Entry

新規 entry はしない。270 円以下で 100 株なら損失寄与は限定的だが、Q1 CF 非開示のまま買うほどの edge はない。Q2 CF で OCF / FCF durability が確認できた場合だけ、改めて 270 円以下を上限に検討する。

## Exit

未保有のため exit なし。Q2 CF で FCF / OCF durability が確認できた場合だけ、target 340 円、stop 230 円を改めて使う。

## Invalidation

- Q2 CF で営業 CF が弱く、2025年12月期 FCF が一過性だったと分かる。
- 在庫と売上債権が積み上がり、売上成長が止まる。
- 通期営業利益の減益計画が保守的ではなく構造悪化だと判断される。

## Position size

conviction は medium だが、research_decision は deferred。paper proxy、real order intent、ADV participation はすべて 0。Q2 CF 後の follow-up issue #166 で再確認する。
