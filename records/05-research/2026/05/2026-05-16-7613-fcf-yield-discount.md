---
ticker: '7613'
name: シークス
playbook_id: fcf-yield-discount
playbook_ref:
  ref_path: records/_playbooks/fcf-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
research_decision:
  outcome: rejected
  posture: dropped
  rejection_reason: lower_priority_after_q1_profit_and_cf_check
  reason_code: q1_fcf_durability_not_confirmed
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-08.yaml
  ticker: '7613'
published_at: '2026-05-16T10:40:00+09:00'
recorded_at: '2026-05-16T10:40:00+09:00'
tradable_at: null
position_sizing_overlay:
  paper_proxy_position_size_yen: 0
  real_order_intent_yen: 0
  adv_participation_pct: 0.0
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
macro_context_ref: records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml
macro_context_fit:
  context_freshness: current
  fit: not_matched
  decision_effect: proceed
  required_checks: []
corporate_action_check:
  checked: true
  result: none
  note: Checked during research review.
---

# Research: 2026-05-16 7613 シークス fcf-yield-discount

## Thesis

7613 は今回の Q1 後確認では追加投入候補から外す。2026-05-08 candidates の FCF yield 35.0%、OCF yield 40.1%、PBR 0.60、P/S 0.23 は割安に見えるが、2026-05-13 Q1 は売上高 +2.0%、営業利益 -5.4%。決算説明資料の為替影響除きでは売上高 -2.9%、営業利益 -6.1% で、営業面の底打ちが弱い。

net debt と棚卸資産は悪化していない。そこは継続 watch に値するが、Q1 CF 計算書がなく FCF durability を確認できず、6835 のような利益進捗と net cash の組み合わせもない。6310 より debt risk は軽いが、営業利益減益と FX-adjusted 減収を見て、今回の追加投入順位からは落とす。

## Macro context

- 判定: not_matched / proceed。
- Sector: 卸売業。
- Source: `records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml`。
- 注意: 卸売業は商社 mix が広く、sector 一括 tailwind ではない。macro は採用を押し上げない。

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
