---
ticker: '9692'
name: シーイーシー
playbook_id: sales-discount-growth
playbook_ref:
  ref_path: records/_playbooks/sales-discount-growth/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: approved
  posture: act_now
  reason_code: pre_refactor_history_restored_and_payoff_pass
candidate_ref:
  candidates_ref: records/02-candidates/2026/05/2026-05-01.yaml
  ticker: '9692'
published_at: '2026-05-05T20:05:00+09:00'
recorded_at: '2026-05-05T20:05:00+09:00'
tradable_at: '2026-05-07T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 200000
  adv_participation_pct: 0.1538
thesis_payoff:
  max_entry_price_yen: 2000
  fair_value_yen: 2300
  invalidation_conditions:
  - 成長鈍化が P/S と配当支えを上回る。
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 15.0
  expected_downside_pct: 10.0
  risk_reward_ratio: 1.5
market_cap_oku: 678
sector_33: 情報・通信業
avg_turnover_oku: 1.3
valuation:
  per_forward: null
  per_trailing: 11.64
  pbr: 1.41
  ev_ebitda: 5.3
  p_s: 1.03
  pcfr: 11.6
  ocf_yield: 0.0859
  fcf_yield: null
  net_cash_to_market_cap: 0.3661
  cash_to_market_cap: 0.3715
  price_to_equity: 1.5924
  equity_ratio: 0.6848
  primary_metric:
  - p_s
corporate_action_check:
  checked: true
  result: none
  note: Checked during research review.
---

# リサーチ: 2026-05-05 9692 CEC sales-discount-growth

## Thesis（投資仮説）

CEC は、リファクタリング前に承認済みだったメモを現在の records に戻したもの。旧 records では DTS と並んで選定されており、現在の broker position snapshot では 100 株が 1,953 円で約定している。

2026-05-01 candidate snapshot 上では、DTS より仮説は強い。P/S 1.03、売上 YoY +17.2%、OCF yield 8.59%、net cash / market cap 36.61%、配当支えがある。ただし単一銘柄としては控えめなサイズに留め、tactical allocation を大きく張る位置づけではない。

## Sales / P/S snapshot（売上・P/S）

| 指標 | 値 |
| --- | ---: |
| P/S | 1.03 |
| P/S sector gap | -54.4% |
| Sales TTM | 65,882 百万円 |
| Sales YoY | +17.2% |
| 営業利益 | 7,338 百万円 |
| OCF yield | 8.59% |
| Net cash / market cap | 36.61% |

2026-05-01 candidate row を canonical fact source とする。

## Margin bridge（利益率の見立て）

売上成長は強く、営業利益もプラス。margin expansion が中心仮説ではなく、売上成長、低 P/S、net cash、株主還元の組み合わせで見る。

## CFO / loss narrowing（CFO・赤字縮小）

CEC は営業黒字のため、赤字縮小は gate 条件ではない。OCF yield は補助材料として有用だが、この trade を cash-flow discount playbook に変えるほどではない。

## Growth durability（成長持続性）

次回 review では、2027/1 期の成長が大きく鈍化していないかを確認する。会社 guidance が弱い成長や service margin の悪化を示す場合、P/S discount は正当化される可能性がある。

## Shareholder return（株主還元）

配当支えと過去の自己株式取得は、catalyst の補助 evidence として扱う。

## Entry（エントリー）

2026-05-07 に 100 株、1,953 円で約定。旧 order guard は 2,000 円で、実際の約定価格は guard を下回った。

## Exit（出口）

目標株価は 2,300 円。損切り価格は 1,800 円。time stop は 40 営業日。2026-06-11 の 1Q window 前に別途 review する。

## Invalidation（無効化条件）

- 売上成長が鈍化し、P/S discount が正当化される。
- OCF が弱まり、配当支えの信頼度が下がる。
- Macro context が headwind に悪化する。

## Position size（ポジションサイズ）

- 数量: 100 株。
- 判断時の guarded notional: 200,000 円。
- 実際の entry notional: 195,300 円。
- 実資金 5,000,000 円に対する concentration: 3.91%。
- tactical budget 1,000,000 円に対する concentration: 19.53%。
- この record は実約定済みポジションを現在の records に戻すもの。月次 retro では、リファクタリング前 record の復元ケースとして確認する。
