---
ticker: '9470'
name: 学研ホールディングス
playbook_id: sales-discount-growth
playbook_ref:
  ref_path: records/_playbooks/sales-discount-growth/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: user_position_confirmed_after_screening_and_q1_growth_check
candidate_ref:
  candidates_ref: records/02-candidates/2026/05/2026-05-01.yaml
  ticker: '9470'
published_at: '2026-05-08T00:00:00+09:00'
recorded_at: '2026-05-08T00:00:00+09:00'
tradable_at: '2026-05-07T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 196400
  adv_participation_pct: 0.1637
thesis_payoff:
  max_entry_price_yen: 982
  fair_value_yen: 1100
  invalidation_conditions:
  - 2026-05-15 2Q earnings が売上成長または margin recovery を無効化する。
  entry_trigger: post_entry_review_required
  expected_upside_pct: 12.02
  expected_downside_pct: 6.31
  risk_reward_ratio: 1.9
market_cap_oku: 432
sector_33: 情報・通信業
avg_turnover_oku: 1.2
valuation:
  per_forward: 9.99
  per_trailing: 99.28
  pbr: null
  ev_ebitda: 4.8
  p_s: 0.21
  pcfr: null
  ocf_yield: null
  fcf_yield: null
  net_cash_to_market_cap: -0.4335
  cash_to_market_cap: null
  price_to_equity: 0.7342
  equity_ratio: 0.4111
  primary_metric:
  - p_s
corporate_action_check:
  checked: true
  result: none
  note: Checked during research review.
---

# リサーチ: 2026-05-08 9470 学研ホールディングス sales-discount-growth

## Thesis（投資仮説）

ユーザーは 2026-05-01 screening result を見た後に 9470 を買い付けた。現在の records に買付履歴がなかったため、この memo では現在保有している事実と、trade ledger に載せるための research check を記録する。broker position snapshot では 200 株、982 円の保有が確認されている。

candidate fact は明確。9470 は P/S 0.21、P/S sector gap -90.5%、売上 YoY +6.0%、営業黒字で sales-discount-growth に該当した。公式 FY2026 Q1 IR では、売上 +6.0% YoY、EBITDA +38.9%、営業利益 +85.7% が確認でき、2 つ目の evidence component として使える。一方、Q1 の親会社株主帰属利益は減少しており、margin quality も未確認のため、2026-05-15 FY2026 Q2 決算後に必ず確認する。

## Sales / P/S snapshot（売上・P/S）

| 指標 | 値 |
| --- | ---: |
| P/S | 0.21 |
| P/S sector gap | -90.5% |
| Sales TTM | 201,894 百万円 |
| Sales YoY | +6.0% |
| 営業利益 | 1,203 百万円 |
| EV/EBITDA | 4.8 |
| Net cash / market cap | -43.35% |

2026-05-01 candidate row を canonical fact source とする。

## Latest IR check（最新 IR 確認）

公式 FY2026 Q1 IR により、2026-05-08 時点で sales-growth screen が古くなっていないことを確認した。売上は 487.1 億円、EBITDA は 23.9 億円、営業利益は 12.0 億円。営業利益回復を 2 つ目の sizing evidence とする。リスク evidence は、親会社株主帰属利益が 50.4% 減少していることと、次の FY2026 Q2 発表が 2026-05-15 に予定されていること。

## Margin bridge（利益率の見立て）

screening row は低 P/S と売上成長を支持するが、収益性は薄い。次回 review では、低 P/S が構造的な低 margin の反映なのか、一時的な discount なのかを確認する。

## CFO / loss narrowing（CFO・赤字縮小）

営業利益はプラス。candidate row では OCF yield は unavailable だが、EDINET OCF TTM は存在する。entry 前に十分確認していないため、cash conversion は open check として残す。

## Growth durability（成長持続性）

次回 review では 2026-05-15 FY2026 Q2 release を読み、教育・医療福祉領域の成長が維持されているかを確認する。

## Shareholder return（株主還元）

この memo では shareholder-return catalyst は確認していない。会社 IR で確認するまで、株主還元を sizing argument として使わない。

## Entry（エントリー）

filled entry は 200 株、982 円。prompt には broker execution timestamp が含まれていなかったため、trade record では reopen context と 982 円の entry price に基づいて 2026-05-07 を使う。

## Exit（出口）

初期 review target は 1,100 円。損切り価格は 920 円。価格にかかわらず、2026-05-15 決算後に即時 review する。

## Invalidation（無効化条件）

- 2026-05-15 2Q earnings で売上成長または margin recovery が弱まる。
- OCF / working capital quality が悪い。
- P/S discount が一時的な見落としではなく、構造的な低 margin で説明できる。
- FY2026 Q2 で売上成長または margin recovery が確認できない。

## Position size（ポジションサイズ）

- 数量: 200 株。
- 判断時の guarded notional: 196,400 円。
- 実際の entry notional: 196,400 円。
- 実資金 5,000,000 円に対する concentration: 3.93%。
- tactical budget 1,000,000 円に対する concentration: 19.64%。
