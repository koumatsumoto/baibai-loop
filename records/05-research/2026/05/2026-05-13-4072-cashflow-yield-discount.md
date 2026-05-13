---
ticker: '4072'
name: 電算システムホールディングス
playbook_id: cashflow-yield-discount
playbook_ref:
  ref_path: records/_playbooks/cashflow-yield-discount/2026-05-01T000000+0900.md
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
portfolio_exposure_ref:
  ref_path: records/_portfolio-exposure/2026/05/2026-05-05T203000+0900.yaml
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-08-4072-cashflow-yield-discount
- source: candidate
  evidence_hit_id: candidate-2026-05-08-4072-fcf-yield-discount
- source: candidate
  evidence_hit_id: candidate-2026-05-08-4072-sales-discount-growth
research_decision:
  outcome: deferred
  posture: wait_for_event
  deferral_reason: data_gap
  revisit:
    trigger: fy2026_q2_cash_flow_statement
    revisit_after: '2026-08-12'
    expires_at: '2026-08-31'
    blocking_conditions:
    - Q2 の半期 CF で営業 CF / FCF / working capital を確認し、2025年12月期の OCF / FCF yield が一過性ではないことを確認する。
    - 収納代行サービスの価格改定後、仕入単価・金利上昇による margin pressure が正常化するか確認する。
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-08.yaml
  screen_run_id: screening-20260508
  ticker: '4072'
  candidate_id: candidate-2026-05-08-4072
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
- records/02-brief/2026/05/2026-05-10-world-weekly-us-jobs-nikkei-wti.yaml
- records/02-brief/2026/05/2026-05-13-world-daily-us-cpi-boj-opinions.yaml
ai_draft: false
published_at: '2026-05-13T22:01:48+09:00'
recorded_at: '2026-05-13T22:01:48+09:00'
tradable_at: '2026-08-13T09:00:00+09:00'
macro_regime_gate:
  aggregate_status: supportive
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs:
  - scope: sector
    key: 情報・通信業
    status: supportive
    source_ref: records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml
    weight_or_materiality: high
    confidence: low
policy_overrides: []
external_refs:
- ref_path: records/_external/densan-system-hd/2026-05-13-fy2026-q1-official-ir.md
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-08-4072-cashflow-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-13T22:01:48+09:00'
  reason_code: source_status_ok
- evidence_hit_id: candidate-2026-05-08-4072-fcf-yield-discount
  effective_sizing_eligible: true
  evaluated_at: '2026-05-13T22:01:48+09:00'
  reason_code: source_status_ok
- evidence_hit_id: candidate-2026-05-08-4072-sales-discount-growth
  effective_sizing_eligible: true
  evaluated_at: '2026-05-13T22:01:48+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-4072-fy2026-q1-cloud-growth
  decision_role: freshness_adjustment
  evidence_polarity: supports
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/densan-system-hd/2026-05-13-fy2026-q1-official-ir.md
  recorded_at: '2026-05-13T22:01:48+09:00'
- evidence_hit_id: research-4072-q1-payment-margin-pressure
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/densan-system-hd/2026-05-13-fy2026-q1-official-ir.md
  recorded_at: '2026-05-13T22:01:48+09:00'
- evidence_hit_id: research-4072-q1-cash-flow-gap
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/densan-system-hd/2026-05-13-fy2026-q1-official-ir.md
  recorded_at: '2026-05-13T22:01:48+09:00'
independent_evidence_count: 3
raw_playbook_concurrence_count: 3
sizing_eligible_playbook_concurrence_count: 3
raw_evidence_family_count: 2
sizing_eligible_evidence_family_count: 2
conviction_tier: high
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
    hypothetical_paper_proxy_position_size_oku: 0.015
    hypothetical_paper_proxy_position_size_yen: 1500000
thesis_payoff:
  time_horizon_bd: 60
  invalidation_conditions:
  - Q2 で営業 CF / FCF が弱く、2025年12月期の OCF / FCF が運転資本の一時要因だった可能性が高まる。
  - 収納代行サービスの営業減益が単価改定後も続き、金利上昇・仕入単価上昇を価格転嫁できない。
  - 情報サービスの Google / SI 成長が鈍化し、通期営業利益 +0.7% 計画を上回る確度が下がる。
  entry_trigger: q2_cash_flow_and_payment_margin_confirmation
tracking:
  mode: re_examination
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 315
sector_33: 情報・通信業
avg_turnover_oku: 1.2
valuation:
  per_forward: null
  per_trailing: 10.77
  pbr: 1.27
  ev_ebitda: 2.1
  p_s: 0.46
  pcfr: 7.6
  ocf_yield: 0.1322
  fcf_yield: 0.1121
  net_cash_to_market_cap: 0.6987
  cash_to_market_cap: 0.558
  price_to_equity: 1.2664
  equity_ratio: 0.36
  primary_metric:
  - ocf_yield
  - fcf_yield
  - p_s
---

# Research: 2026-05-13 4072 電算システムホールディングス cashflow-yield-discount

## Thesis

4072 電算システムホールディングスは、2026-05-08 screening で `cashflow-yield-discount`、`fcf-yield-discount`、`sales-discount-growth` が同時 hit した。候補時点の TTM OCF yield は 13.2%、FCF yield は 11.2%、P/S は 0.46、PER は 10.77、net cash / market cap は 69.9% で、quality / stability 候補として継続確認に値する。

Q1 公式 IR では、売上高 +10.8%、営業利益 +13.2%、情報サービス営業利益 +52.1%、Google ビジネス売上高 +30.6%、通期営業利益進捗 33.6% が確認できた。したがって `reject` ではなく継続 research とする。一方、Q1 では四半期連結キャッシュ・フロー計算書が作成されておらず、収納代行サービスは仕入単価・金利上昇・新規投資で営業減益である。decision は `deferred / wait_for_event` とし、Q2 の半期 CF と価格改定後の margin を待つ。

## Macro regime gate

- Gate: supportive / pass。
- Sector: 情報・通信業。
- 参照: `records/03-outlook/2026/05/outlook-2026-05-10-post-us-jobs-nikkei-wti.yaml`。
- Outlook は情報・通信業を supportive としている。4072 の Google Workspace / Google Cloud、公共 DX、AI PoC はこの gate と整合する。ただし、今回の採用根拠は AI theme ではなく、cashflow yield、FCF yield、P/S discount、Q1 の事業進捗である。

## Cashflow snapshot

| 指標 | 値 | 判定 |
| --- | ---: | --- |
| OCF TTM | 4,165 百万円 | candidate primary signal |
| OCF yield | 13.2% | cashflow-yield-discount hit |
| FCF TTM | 3,533 百万円 | fcf-yield-discount hit |
| FCF yield | 11.2% | strong |
| CFO YoY | +37.9% | 2025年12月期 source |
| P/S | 0.46 | sales-discount-growth hit |
| PER trailing | 10.77 | 補助 |
| Company-adjusted net cash | 17,030 百万円 | 2026 Q1 official IR |

Source: `records/04-candidates/2026/05/2026-05-08.yaml` and `records/_external/densan-system-hd/2026-05-13-fy2026-q1-official-ir.md`。

Q1 は売上・利益では positive だが、営業 CF は開示されない。候補時点の market cap 31,500 百万円に対し、会社定義 net cash は 17,030 百万円で、net cash / market cap は約 54.1%。収納代行預り金を控除した会社定義でも net cash は大きいが、Q1 は短期借入金と収納代行預り金が増えており、2025年12月期の FCF が再現可能かは半期 CF で確認する。

## 運転資本確認

4072 は収納代行サービスを持つため、現金及び預金、金銭の信託、収納代行預り金が大きく、表面的な cash / net cash をそのまま投資余力と見ない。Q1 では現金及び預金 23,679 百万円、金銭の信託 22,603 百万円、収納代行預り金 25,556 百万円、短期借入金 3,100 百万円、有利子負債 3,696 百万円が確認された。会社はこれらを控除した net cash を 17,030 百万円としている。

Q2 で確認する条件:

- 半期営業 CF が 2025年12月期の OCF / FCF thesis と整合する。
- 収納代行預り金と金銭の信託の増加が、実質的な資金余力を過大表示していない。
- 短期借入金の増加が一時的な資金繰りで、収益性悪化や回収遅延を示していない。
- 2026-04 からの価格改定後、仕入単価・金利上昇による margin pressure が緩和する。

## Capex / FCF quality

候補時点の FCF TTM は 3,533 百万円、FCF yield は 11.2%。2025年12月期の capex TTM は 632 百万円、減価償却費は 829 百万円で、FCF は設備投資控除後でも残っている。ただし、Q1 では CF 計算書がないため、営業 CF、投資 CF、維持投資、working capital の更新確認ができない。

FCF thesis は Q2 の半期 CF で営業 CF と投資 CF を確認するまで approved にしない。Q1 の減価償却費は 176 百万円、のれん償却額は 40 百万円であり、会計利益から現金創出への変換は半期 CF で再確認する。

## Earnings quality

Q1 の売上高は 17,223 百万円、営業利益は 1,227 百万円。前年同期比では売上 +10.8%、営業利益 +13.2%。通期営業利益予想 3,650 百万円に対する進捗率は 33.6% で、会社は通期予想を据え置いた。

情報サービスは明確に positive。売上高 11,486 百万円、営業利益 638 百万円で、営業利益は前年同期比 +52.1%。クラウドサービス・ライセンス販売は売上高 +23.3%、Google ビジネス売上高は +30.6%、Google Workspace 導入企業数は 2,398 社で前年同期比 +8.9%。SI / ソフト開発ではオートオークション業務システムや公共分野 DX 大型案件も確認できた。

収納代行サービスは mixed。売上高は +0.3% で横ばい、営業利益は -12.1%。地方自治体向けコンビニ収納代行の処理件数 +11.3%、オンライン決済処理件数 +6.8% は positive だが、仕入単価上昇、金利上昇に伴う資金管理コスト、新規投資をまだ吸収できていない。2026-04 からの価格改定が margin に効くかを次回確認する。

## Shareholder return

2026年12月期の年間配当予想は 100 円で、2025年12月期の 90 円から増配計画。内訳は中間 50 円、期末 50 円。100 株以上を 1 年以上保有する株主には 3,000 円相当の地域特産品または寄付の優待もある。

株主還元は long-hold fallback の補助材料だが、この thesis の主因は CF yield と P/S discount である。配当・優待だけでは approved にしない。

## Entry

現時点では entry しない。Q1 後の判断は `継続 research` だが、cashflow-yield-discount thesis の中核である営業 CF が Q1 で更新されないため、Q2 の半期 CF を待つ。次の確認日は、2025年12月期第2四半期決算が 2025-08-12 に開示されていたことを踏まえ、2026-08-12 を暫定 revisit after とする。

Q2 で半期営業 CF、FCF、収納代行 margin、価格改定効果が確認できれば、候補時点の 3 evidence hit は high tier として再評価する。候補時点の market cap 31,500 百万円から会社定義 net cash 17,030 百万円を控除した ex-cash market cap は約 14,470 百万円で、通期営業利益予想 3,650 百万円に対する ex-cash market cap / OP は約 4.0x。valuation は継続 research に十分だが、Q1 CF 不在のまま注文しない。

## Exit

未保有のため exit 条件は未設定。Q2 後に approved へ進める場合は、max entry、target、stop loss、time stop をその時点の価格、流動性、半期 CF の再計算で設定する。

## Invalidation

- Q2 で営業 CF / FCF が弱く、2025年12月期の CFO +37.9% が一過性だった可能性が高まる。
- 収納代行サービスの営業減益が価格改定後も続き、仕入単価・金利上昇を価格転嫁できない。
- Google Workspace / Google Cloud、SI、公共 DX の伸びが鈍化し、情報サービスの営業利益率改善が続かない。
- 通期営業利益予想 +0.7% の据え置きに対し、Q2 以降の進捗が鈍化する。
- 情報・通信業の macro gate が adverse へ悪化する。

## Position size

- **decision**: deferred / wait_for_event。
- **paper proxy size**: 0 円。
- **real order intent**: 0 円。
- **hypothetical if approved**: high tier の候補だが、Q2 CF 確認前は注文しない。
- **binding cap**: data gap。Q1 で営業 CF が開示されていない。

## Source verification log

| external_ref | 採用 | 修正 | 未採用 |
| --- | --- | --- | --- |
| `records/_external/densan-system-hd/2026-05-13-fy2026-q1-official-ir.md` | Q1 売上・営業利益、segment 別売上・営業利益、Google business KPI、会社定義 net cash、通期予想、配当予想、Q1 CF 未作成 | 候補時点の net cash / market cap 69.9% は、Q1 公式資料の収納代行預り金控除後 net cash 17,030 百万円で保守的に見直し | AI / DX、stablecoin、Google award は補助材料にとどめ、注文判断や sizing の主因にはしない |
