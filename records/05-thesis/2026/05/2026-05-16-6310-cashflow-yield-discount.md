---
ticker: '6310'
name: 井関農機
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
  effective_from: '2026-05-01T00:00:00+09:00'
thesis_decision:
  outcome: rejected
  posture: dropped
  rejection_reason: post_q1_cashflow_thesis_not_cleared
  reason_code: q1_working_capital_and_net_debt_risk
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-08.yaml
  ticker: '6310'
published_at: '2026-05-16T10:30:00+09:00'
recorded_at: '2026-05-16T10:30:00+09:00'
tradable_at: null
position_sizing_overlay:
  estimated_real_order_notional_yen: 0
  adv_participation_pct: 0
thesis_payoff:
  max_entry_price_yen: 1750
  fair_value_yen: 2100
  invalidation_conditions:
  - FY2026 Q1 operating cash-flow thesis deteriorates.
  - Net debt remains large relative to market cap after the event check.
  entry_trigger: rejected_after_q1
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

# Research: 2026-05-16 6310 井関農機 cashflow-yield-discount

## Thesis

6310 は今回の 2026-05-15 Q1 後確認では追加投入候補から外す。売上高 +11.5%、営業利益 +88.5% は良いが、採用根拠の中心だった OCF yield 56.5% が Q1 の営業 CF -8,933 百万円で確認できなかった。Q1 特有の季節性はあるが、売上債権増加、棚卸資産増加、短期借入金増加が同時に出ており、net debt risk を抱えたまま 1 単元 17 万円台の starter を作る理由は弱い。

Long-hold fallback は低-中のまま。P/S 0.22、PBR 0.55 は割安だが、農機の薄い margin、在庫、販売金融、金利感応度を上回る cash conversion がまだ見えていない。

## Macro context

- 判定: mixed / proceed。
- Sector: 機械。
- Source: `records/01-macro-context/2026/05/macro-context-2026-05-04-screening.yaml`。
- 注意: macro context の機械 mixed は半導体製造装置・外需設備投資の色が強い。井関農機への直接度は低いため、macro context fit はこの rejection を覆さない。

## Cashflow snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| Q1 売上高 | 51,471 百万円 | +11.5%、sales thesis は継続 |
| Q1 営業利益 | 2,603 百万円 | +88.5%、利益進捗は良い |
| Q1 営業 CF | -8,933 百万円 | primary cashflow thesis 未達 |
| 売上債権増加 | -11,732 百万円 | working capital outflow |
| 棚卸資産増加 | -704 百万円 | 圧縮確認には至らず |
| 現金及び預金 | 9,538 百万円 | 2025-12 末 12,891 百万円から減少 |
| 短期借入金 | 39,215 百万円 | 2025-12 末 28,738 百万円から増加 |

2026-05-08 candidates の OCF TTM 23,456 百万円、OCF yield 56.5% は強いが、Q1 で反証確認が必要だった。今回の Q1 は営業 CF がマイナスで、営業増益だけでは cashflow-yield-discount の採用条件を満たさない。

## 運転資本確認

Q1 の営業 CF マイナスは会社説明上は季節性を含む。ただし、この research の論点は「運転資本の巻き戻りがないこと」だったため、季節性を理由に通過扱いにはしない。売上債権が大きく増え、棚卸資産も小幅ながら増加したため、working capital quality は未確認ではなく弱い側に倒す。

## Capex / FCF quality

Q1 投資 CF は -2,667 百万円。Project Z 関連を含む投資負担が続く。候補 snapshot では capex tag が取れず FCF yield は unavailable だったため、営業 CF が確認できない局面で FCF を根拠に補強しない。

## Earnings quality

売上と営業利益の進捗は良い。国内は需要取り込み、海外は欧州販売拡大が説明され、価格改定と Project Z 効果も出ている。一方、親会社株主帰属四半期純利益は -1.9% で、営業外・特別・税金後まで見ると営業増益ほどの質は残らない。

## Shareholder return

年間配当予想は 45 円で据え置き。配当は下値要素だが、net debt と Q1 cash out を補って starter を正当化するほどではない。

## Entry

新規 entry はしない。前回の仮条件だった 1,750 円以下でも、Q1 で cashflow thesis が通過していないため注文意図は作らない。

## Exit

未保有のため exit なし。既存候補リスト上は、今回の Q1 後タスクでは reject とする。

## Invalidation

- Q1 営業 CF が -8,933 百万円。
- 売上債権と棚卸資産が増加し、working capital の反証確認を通過しなかった。
- 短期借入金が期末比で増加し、net debt risk が残った。

## Position size

thesis_decision は rejected。paper proxy、real order intent、ADV participation はすべて 0。追加投入候補化はしない。
