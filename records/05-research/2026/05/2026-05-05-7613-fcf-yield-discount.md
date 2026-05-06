---
ticker: '7613'
name: シークス
playbook_id: fcf-yield-discount
playbook_snapshot:
  ref_path: records/_playbooks/fcf-yield-discount/2026-05-01T000000+0900.md
  content_sha256: sha256:f3d5fbf92cfa4b4663b59837b6ed819031df8a5b153e636877c815355154fbc6
  effective_from: '2026-05-01T00:00:00+09:00'
policy_snapshot:
  ref_path: records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md
  content_sha256: sha256:4d8b769749184e6733a4a698000a21070f98d681beacf30bb30568c917f6372a
  effective_from: '2026-05-01T00:00:00+09:00'
policy_applicability: active
calendars_snapshot:
  business_days:
    ref_path: records/_calendars/business-days/2026-05.yaml
    content_sha256: sha256:cd3ddf5dcb6b4de0595547c033d9be68920272be45bf7a358f42465caa17c273
  events:
    ref_path: records/_calendars/events/2026-05.yaml
    content_sha256: sha256:583cf61a16375dfedb766659911daa435b9b8f9d40cd1e26721d3b734ffb0ea6
  corporate_actions:
    ref_path: records/_calendars/corporate-actions/2026-05.yaml
    content_sha256: sha256:5b1f2487807f590acdc5784ac2e84a851aa7264a03c1ffc3f84c5ebfbdcd761c
portfolio_exposure_snapshot_ref:
  ref_path: records/_portfolio-exposure/2026/05/2026-05-05T203000+0900.yaml
  content_sha256: sha256:6c7bc24d8b753da299984cf647300b705e6f89e37266d10695b0c16efb534329
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-01-7613-fcf-yield-discount
- source: candidate
  evidence_hit_id: candidate-2026-05-01-7613-cashflow-yield-discount
research_decision:
  outcome: deferred
  posture: wait_for_event
  deferral_reason: event_pending
  revisit:
    trigger: thesis_update
    revisit_after: '2026-05-12'
    expires_at: '2026-06-30'
    blocking_conditions:
    - event risk must clear
candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
  screen_run_id: screening-20260501-b2e37953
  ticker: '7613'
  candidate_id: candidate-2026-05-01-7613
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: true
published_at: '2026-05-05T19:45:00+09:00'
recorded_at: '2026-05-05T19:45:00+09:00'
tradable_at: '2026-05-07T09:00:00+09:00'
macro_regime_gate:
  aggregate_status: supportive
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs:
  - scope: sector
    key: 情報・通信業
    status: supportive
    source_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
    valid_until: '2026-05-13'
    weight_or_materiality: high
    confidence: high
policy_overrides: []
external_refs: []
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-01-7613-fcf-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T19:45:00+09:00'
  reason_code: source_status_ok
- evidence_hit_id: candidate-2026-05-01-7613-cashflow-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T19:45:00+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-7613-risk-review
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs: []
  recorded_at: '2026-05-05T19:45:00+09:00'
independent_evidence_count: 2
raw_playbook_concurrence_count: 2
sizing_eligible_playbook_concurrence_count: 2
raw_evidence_family_count: 1
sizing_eligible_evidence_family_count: 1
conviction_tier: medium
conviction_tier_path: count_breadth
depth_verification_ref: null
position_sizing_overlay:
  paper_proxy_position_size_oku: 0.0
  paper_proxy_position_size_yen: 0
  real_order_intent_yen: null
  adv_participation_pct: 0.0
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual:
  if_approved:
    hypothetical_paper_proxy_position_size_oku: 0.01
    hypothetical_paper_proxy_position_size_yen: 1000000
thesis_payoff:
  max_entry_price_yen: 1300
  target_price_yen: 1550
  stop_loss_yen: 1150
  time_horizon_bd: 40
  invalidation_conditions:
  - FCF strength is explained by working-capital timing only.
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 19.23
  expected_downside_pct: 11.54
  risk_reward_ratio: 1.67
tracking:
  mode: re_examination
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 647
sector_33: 卸売業
avg_turnover_oku: 3.4
valuation:
  per_forward: null
  per_trailing: 24.31
  pbr: 0.59
  ev_ebitda: 4.4
  p_s: 0.22
  pcfr: 2.4
  ocf_yield: 0.4101
  fcf_yield: 0.3579
  cash_to_market_cap: 0.4604
  net_cash_to_market_cap: -0.2268
  price_to_equity: 0.6235
  equity_ratio: 0.4993
  primary_metric:
  - fcf_yield
  - ocf_yield
---

# Research: 2026-05-05 7613 シークス fcf-yield-discount

**成分**: 個別銘柄リサーチ

**Playbook id**: fcf-yield-discount

## Thesis

7613 シークスは、2026-05-01 candidates で `fcf-yield-discount` と `cashflow-yield-discount` が同時 hit した。EDINET CSV-derived metrics では FCF yield 35.8%、OCF yield 41.0%、PBR 0.59、P/S 0.22 で、PER/PBR 中心の旧 screening では見えにくかった「設備投資後 CF に対して安い」候補として拾えている。

ただし decision は deferred。2025/12 期は売上 YoY -4.2%、親会社株主帰属当期純利益 -33.7%、EDINET net cash ratio は -22.7% で net debt。FCF は強いが、運転資本改善や投資抑制による一過性の可能性を確認するまで approved にしない。

## Macro regime gate

- **判定**: neutral
- **業種**: 卸売業
- **outlook_ref**: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
- **根拠**: 2026-05-04 outlook は卸売業を neutral としている。総合商社は資源と非資源の mix があり、中堅卸売業も製品 mix が多様なため、sector 一括 supportive とはしない。
- **保守側判定**: neutral。FCF の個別強度を見に行くが、macro supportive で押し上げる銘柄ではない。

## FCF snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| EDINET CFO | 26,539 百万円 | primary cash source |
| Capex | 3,376 百万円 | `purchase_of_fixed_assets` |
| FCF | 23,163 百万円 | CFO - capex |
| FCF yield | 35.8% | evidence hit hit |
| J-Quants OCF yield | 41.0% | supporting evidence hit |
| CFO YoY | +14.9% | 悪化なし |
| PBR | 0.59 | 資産面の補助 |
| EV/EBITDA | 4.4 | 補助 |
| Net cash / market cap | -22.7% | cash-rich ではなく net debt |

Source:

- candidates: records/04-candidates/2026/05/2026-05-01.yaml
- EDINET source: `S100XUJ0`, document type `120`, submitted 2026-03-27 15:30, period 2025-01-01 to 2025-12-31
- 会社決算短信: https://www.siix.co.jp/wordpress/wp-content/uploads/2026/02/earnings_260212.pdf
- 会社決算短信一覧: https://www.siix.co.jp/ir/library/statements/

## Capex quality

Capex は 3,376 百万円で、EDINET metric の source は `purchase_of_fixed_assets`。CFO 26,539 百万円に対して capex が小さいため、FCF yield が非常に高く出ている。

この点は強みである一方、trap でもある。シークスは EMS / 電子部品関連の商社・製造受託要素を持ち、設備投資サイクルや顧客在庫循環の影響を受ける。2025/12 期の capex が維持投資の通常水準なのか、投資先送りで一時的に低いだけなのかを、決算説明資料または有報の設備投資・減価償却・セグメント情報で確認する必要がある。

## Working capital quality

FCF thesis の最大論点は運転資本。2025/12 期は売上高 289,491 百万円で前年比 -4.2% だが、営業 CF は強く、CFO YoY は +14.9%。売上減少局面では売掛金・棚卸資産の圧縮で CFO が押し上げられることがある。

したがって、approved 条件は以下:

- 売掛債権、棚卸資産、仕入債務の増減で CFO 増加が一過性ではないことを確認する。
- EMS 顧客の在庫調整が終わり、受注・売上が再加速する兆候があることを確認する。
- 税金・補助金・為替などの一過性 cash-in が FCF の主因ではないことを確認する。

## Earnings quality

会社決算短信では、2025/12 期の売上高は 289,491 百万円、営業利益は 8,853 百万円、親会社株主帰属当期純利益は 2,488 百万円。営業利益は前年比 +3.4% だが、売上は -4.2%、純利益は -33.7%。FCF が強い一方で、損益面の成長は明確ではない。

このため thesis は「成長株の rerating」ではなく、「低 PBR / 低 P/S / 高 FCF yield の value recovery」。営業利益率改善が継続するか、売上減少が底打ちするかを見ないと、FCF yield だけで強く採用するのは早い。

## Shareholder return

| 項目 | 確認結果 | 判定 |
| --- | --- | --- |
| 配当実績 | 2025/12 期 年間 49 円 | positive |
| 配当性向 | 2025/12 期 92.8% | 減配リスク確認が必要 |
| 2026/12 期予想 | 決算短信上は年間 50 円 | 下値支え |
| 自社株買い | この research 時点の確認 source では明確な新規 catalyst なし | neutral |

配当利回りは候補化の primary evidence hit ではないが、FCF thesis の裏付けとして重要。2025/12 期の配当性向は高く、純利益減少下での増配に見えるため、2026/12 期の EPS 回復見通しと減配リスクを確認する。

Source:

- 配当の状況: https://www.siix.co.jp/ir/stock/dividend/
- 会社決算短信: https://www.siix.co.jp/wordpress/wp-content/uploads/2026/02/earnings_260212.pdf

## Entry

deferred 条件:

- 2025/12 期の営業 CF 増加が一過性の運転資本改善だけではないこと。
- 2026/12 期の売上・営業利益計画で、売上減少が底打ちすること。
- capex が通常水準から大きく下振れしただけではないこと。
- net debt の水準が、FCF thesis に対して過大ではないこと。
- 1,300 円近辺で出来高が細らず、FCF yield 20% 超の水準が残ること。

初期 paper proxy は 1.0% まで。evidence hit は FCF + OCF の 2 本だが、net debt と売上減少があるため、multiple evidence hit の最大枠をそのまま使わない。

## Exit

- 利確目安: FCF yield が 15-20% 台へ正常化する水準、または PBR 0.7-0.8 倍への小幅 rerating。
- 損切り目安: 2026/12 期見通しで売上減少が続き、営業利益も悪化する場合。
- 時間切れ: 40 営業日。FCF thesis は決算確認で優劣が出やすいため、価格だけで長く引っ張らない。

## Invalidation

- CFO 増加が在庫圧縮・売掛金回収・税金等の一過性要因で、翌期に巻き戻る可能性が高い。
- capex が投資先送りで、2026/12 期に大きく増える。
- EMS / 電子部品の需要悪化が続き、売上減少が構造化する。
- net debt が増え、配当維持や設備投資に制約が出る。
- 2026/12 期の配当予想 50 円が維持困難になる。

## Position size

- **decision**: deferred
- **primary evidence hit**: fcf-yield-discount
- **supporting evidence hit**: cashflow-yield-discount
- **market cap**: 647 億円
- **avg turnover**: 3.4 億円
- **paper proxy position**: 0.01 億円
- **ADV participation**: 0.2941%
- **許容上限**: 最大 1.0%

### EDINET lane の目的達成検証

7613 は、cash-rich ではなく net debt であるため、J-Quants CashEq proxy だけなら誤読しやすい。一方、EDINET CFO / capex で見ると FCF yield が高く、PER/PBR だけでは拾いにくい「現金創出力に対して安い」候補として浮上する。今回の #88 変更は、cash-rich の false positive を抑えつつ、FCF という別軸でお買い得候補を発見する方向には機能している。

ただし、FCF は運転資本で一時的に膨らむため、screening hit は entry ではない。approved には一次資料で working capital quality と capex normalization を確認する必要がある。
