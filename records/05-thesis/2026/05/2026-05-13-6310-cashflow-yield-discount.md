---
ticker: '6310'
name: 井関農機
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: deferred
  posture: wait_for_event
  deferral_reason: event_pending
  revisit:
    trigger: fy2026_q1_earnings_release
    revisit_after: '2026-05-15'
    expires_at: '2026-06-30'
    blocking_conditions:
    - FY2026 Q1 event risk and working-capital quality must clear.
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-08.yaml
  ticker: '6310'
published_at: '2026-05-13T08:50:00+09:00'
recorded_at: '2026-05-13T08:50:00+09:00'
tradable_at: '2026-05-18T09:00:00+09:00'
position_sizing_overlay:
  estimated_real_order_notional_yen: 0
  adv_participation_pct: 0
thesis_payoff:
  max_entry_price_yen: 1750
  fair_value_yen: 2100
  invalidation_conditions:
  - FY2026 Q1 operating cash-flow thesis deteriorates.
  entry_trigger: post_earnings_revisit
  expected_upside_pct: 20.0
  expected_downside_pct: 11.43
  risk_reward_ratio: 1.75
market_cap_oku: 415
sector_33: 機械
avg_turnover_oku: 2.0
valuation:
  per_forward: null
  per_trailing: 14.81
  pbr: 0.55
  ev_ebitda: 9.6
  p_s: 0.22
  pcfr: 1.8
  ocf_yield: 0.5654
  fcf_yield: null
  net_cash_to_market_cap: -1.1878
  cash_to_market_cap: 0.3095
  price_to_equity: 0.529
  equity_ratio: 0.3744
  primary_metric:
  - ocf_yield
  - p_s
macro_context_ref: records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml
macro_context_fit:
  context_freshness: current
  fit: mixed
  decision_effect: proceed
  required_checks: []
corporate_action_check:
  checked: true
  result: none
  note: Checked during research review.
---

# Research: 2026-05-13 6310 井関農機 cashflow-yield-discount

## Thesis

6310 は今回の 3 銘柄で最大 upside 枠として残す。2026-05-08 candidates で OCF yield 56.5%、P/S 0.22、PBR 0.55、sales YoY +10.3% が同時に成立し、数値上の割安は強い。6835 より upside は大きいが、net debt と Q1 直前リスクがあるため順位は 3 位。今買うのではなく、2026-05-15 Q1 後に運転資本の巻き戻りがなければ starter。

Long-hold fallback は低-中。PBR 0.55 と配当は下支えだが、農機は営業利益率が薄く、在庫・販売金融・金利・為替・物流費に左右される。長期保有へ切り替えるには、CF が一過性でなく、net debt を許容できる利益基盤が確認できることが必要。

AI long-term impact は中立。スマート農機や省人化のテーマはあるが、今回の採用根拠ではない。AI 期待で sizing を上げない。

## Macro context

- 判定: mixed / proceed。
- Sector: 機械。
- Source: `records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml`。
- 注意: macro context の機械 mixed は半導体製造装置・外需設備投資色が強い。井関農機は農業機械で直接度が低いため、macro context fit は mixed だが confidence は low。

## Cashflow snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| OCF TTM | 23,456 百万円 | primary cash source |
| OCF yield | 56.5% | very strong |
| CFO YoY | +165.8% | strong but one-off risk |
| P/S | 0.22 | cheap |
| PBR | 0.55 | cheap |
| PCFR | 1.8 | cheap |
| Net cash / market cap | -118.8% | major risk |
| 有利子負債 | 62,172 百万円 | debt quality check required |

cashflow-yield と sales-discount-growth の 2 evidence hit。3 銘柄内では最も upside が大きい一方、debt と working capital の反証も最大。

## 運転資本確認

農業機械は棚卸資産、売掛債権、販売金融、季節性で CFO が大きく動く。OCF yield 56.5% は強すぎるため、在庫圧縮や売掛金回収の一時的 cash-in だけで作られた可能性を先に疑う。

approved 条件:

- FY2026 Q1 で営業 CF の急反転がない。
- 棚卸資産の再積み上がりがない。
- 販売金融や売掛金回収の遅れが増えていない。
- 会社計画が低 P/S / 低 PBR の構造的理由を示さない。

## Capex / FCF quality

EDINET capex tag が未取得のため FCF yield は unavailable。したがって本銘柄は FCF ではなく OCF thesis。設備投資が後ずれしているだけなら、OCF は維持されない。Q1 後に投資 CF、維持投資、減価償却、構造改革費用を確認する。

## Earnings quality

売上 TTM 185,770 百万円、営業利益 4,225 百万円、sales YoY +10.3%。営業利益率は低く、原材料・為替・物流費の変動で利益が薄くなりやすい。採用するなら、低 P/S / 低 PBR だけでなく、収益性改善が続くことが必要。

## Shareholder return

配当は下値支えになるが primary thesis ではない。net debt が重いため、CF が巻き戻ると配当余力も見直される。株主還元よりも、Q1 の運転資本と財務余力を優先確認する。

Source:

- https://www.iseki.co.jp/ir/
- https://www.iseki.co.jp/ir/support/calendar/

## Entry

2026-05-15 に FY2026 Q1 決算発表と決算説明会があるため、5/13 時点で新規注文しない。Q1 通過後、1,750 円以下で CF thesis が残れば 100 株 starter。1,900 円超へ gap up した場合は追わず、OCF yield を再計算する。

## Exit

利確目安は 2,100 円。PBR 0.6 倍台への小幅 rerating を想定する。損切り目安は 1,550 円。Q1 通過後 40 営業日で CF thesis が見えなければ撤退。

## Invalidation

- FY2026 Q1 で営業 CF が大きく悪化する。
- 棚卸資産、販売金融、売掛金回収遅延で working capital が悪化する。
- Net debt が金利上昇や需要悪化で重くなる。
- 会社計画が弱く、低 P/S / 低 PBR が低収益構造の反映と確認される。
- Macro context が headwind に悪化する。

## Position size

- decision: deferred。
- 実資金 starter: 100 株。5/7 付近の株価 1,756 円なら約 175,600 円。
- 1 単元が tactical budget の 17% 台なので、6835 より大きく張らない。
