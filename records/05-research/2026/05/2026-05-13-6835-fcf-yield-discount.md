---
ticker: '6835'
name: アライドテレシスホールディングス
playbook_id: fcf-yield-discount
playbook_ref:
  ref_path: records/_playbooks/fcf-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
research_decision:
  outcome: deferred
  posture: wait_for_event
  deferral_reason: event_pending
  revisit:
    trigger: fy2026_q1_earnings_release
    revisit_after: '2026-05-15'
    expires_at: '2026-06-30'
    blocking_conditions:
    - FY2026 Q1 event risk must clear.
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-08.yaml
  ticker: '6835'
published_at: '2026-05-13T08:50:00+09:00'
recorded_at: '2026-05-13T08:50:00+09:00'
tradable_at: '2026-05-18T09:00:00+09:00'
position_sizing_overlay:
  paper_proxy_position_size_yen: 0
  real_order_intent_yen: 0
  adv_participation_pct: 0.0
thesis_payoff:
  max_entry_price_yen: 270
  target_price_yen: 340
  stop_loss_yen: 230
  time_horizon_bd: 40
  invalidation_conditions:
  - FY2026 Q1 invalidates FCF / OCF / net cash durability.
  entry_trigger: post_earnings_revisit
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

# Research: 2026-05-13 6835 アライドテレシスホールディングス fcf-yield-discount

## Thesis

6835 は今回の 3 銘柄で post-event starter 枠として残す。理由は、2026-05-08 candidates で FCF yield 22.0%、OCF yield 23.9%、PER 9.76、EV/EBITDA 3.3、net cash / market cap 37.9% が同時に成立し、同じ cash-rich 系で上に出た 3668 / 3632 より営業 cash flow の質が見えるため。100 株の notional が小さく、Q1 後に starter を作りやすい点も実資金に合う。

Long-hold fallback は中程度。net cash と FCF は長期保有の下支えになるが、ネットワーク機器は低成長・競争・海外事業変動があり、成長 compounder ではない。含み損を長期保有へ切り替えるには、Q1 後も FCF / net cash / 配当余力が残ることが条件。

AI long-term impact は機会が中、脅威が低-中。AI / data center 投資の周辺ネットワーク需要は追い風になり得るが、同社が直接の高成長 AI infrastructure 銘柄だとは置かない。

## Macro context

- 判定: not_matched / proceed。
- Sector: 電気機器。
- Source: `records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml`。
- 注意: macro context の電気機器 not_matched は半導体・AI 関連需要の色が強い。6835 はネットワーク機器で直接度は低いので、macro context fit は not_matched とする。

## FCF snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| EDINET OCF TTM | 6,747 百万円 | primary cash source |
| Capex TTM | 526 百万円 | `purchase_of_fixed_assets` |
| FCF TTM | 6,221 百万円 | OCF - capex |
| FCF yield | 22.0% | strong |
| OCF yield | 23.9% | strong |
| CFO YoY | +17.5% | deterioration なし |
| Net cash / market cap | 37.9% | balance sheet support |
| PER trailing | 9.76 | valuation support |

2026-05-08 selection では fcf-yield-discount と cashflow-yield-discount の 2 evidence hit。これは cash pile だけでなく cash generation も拾えている点で、3668 / 3632 より今回の目的に合う。

## Capex quality

Capex tag は取得できており、FCF lane は exact。OCF 6,747 百万円に対して capex 526 百万円のため、FCF が大きく残る。ただし capex が一時的に低いだけなら FCF yield は過大になる。Q1 で開発・サービス化・海外再編に伴う投資 cash out が増えないかを確認する。

## Working capital quality

OCF yield 23.9%、CFO YoY +17.5% は強い。反対に、ネットワーク機器は在庫、売掛金、保守契約、海外子会社の timing で CFO がぶれる。Q1 後の approved 条件は、営業 CF が急落せず、在庫と売掛金が膨らまず、2025/12 期の FCF が一過性でないこと。

## Earnings quality

売上 TTM は 49,950 百万円、営業利益 4,228 百万円、sales YoY +3.1%。高成長ではない。採用理由は rerating growth ではなく、低 multiple + high FCF + net cash の回復余地。Q1 で売上鈍化や margin 悪化が出るなら、低 multiple は妥当と見る。

## Shareholder return

公式 IR では、株主還元・配当方針は財務体質と業績を勘案した安定配当を基本方針としている。2026 年 3-4 月に自己株式取得関連開示があり、capital allocation は補助材料。ただし今回の sizing 根拠は配当・優待ではなく FCF / net cash。

Source:

- https://ir.at-global.com/information
- https://ir.at-global.com/stock
- https://ir.at-global.com/stock03

## Entry

2026-05-15 15:30 の FY2026 Q1 決算前には買わない。Q1 で FCF / OCF / net cash thesis が残れば 100 株 starter。270 円以下なら entry 余地あり。決算後に 300 円超まで gap up した場合は、FCF yield を再計算してから追うか判断する。

## Exit

利確目安は 340 円。FCF yield が 15% 台へ縮む程度の小幅 rerating を想定する。損切り目安は 230 円。決算後 40 営業日で thesis が見えなければ撤退。

## Invalidation

- FY2026 Q1 で営業 CF / FCF が急減する。
- Capex が戻ると FCF が薄くなる。
- Net cash が事業再編・投資・株主還元で急減する。
- 売上成長が止まり、低 multiple が構造的 discount と確認される。
- Macro context が headwind に悪化する。

## Position size

- decision: deferred。
- 実資金 starter: 100 株。5/7 付近の株価 268 円なら約 26,800 円。
- 初期 sizing は実資金 1% 未満。Q1 cash quality が強い場合のみ 200-300 株へ増やす。
