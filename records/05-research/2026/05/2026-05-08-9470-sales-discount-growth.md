---
ticker: '9470'
name: 学研ホールディングス
playbook_id: sales-discount-growth
playbook_snapshot:
  ref_path: records/_playbooks/sales-discount-growth/2026-05-01T000000+0900.md
  content_sha256: sha256:37358359da7bd9a56b029ac89ec734acc2a427c3b0c9a53df1c6a59afe03d4ad
  effective_from: '2026-05-01T00:00:00+09:00'
policy_snapshot:
  ref_path: records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md
  content_sha256: sha256:49f20536a65f4e56091c93277215f640acd537a1a9ab032515e9df90c527e766
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
  content_sha256: sha256:fe16419b85c5a1c29dff609beb06643e87cb6a208ea2581ffebd31e481bc428a
selected_supporting_evidence_refs:
- source: candidate
  evidence_hit_id: candidate-2026-05-01-9470-sales-discount-growth
research_decision:
  outcome: approved
  posture: act_now
  reason_code: user_position_confirmed_after_screening_and_q1_growth_check
candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
  screen_run_id: screening-20260501-b2e37953
  ticker: '9470'
  candidate_id: candidate-2026-05-01-9470
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: false
published_at: '2026-05-08T00:00:00+09:00'
recorded_at: '2026-05-08T00:00:00+09:00'
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
    confidence: low
policy_overrides: []
external_refs:
- ref_path: records/_external/gakken/2026-05-08-fy2026-q1-official-ir.md
  content_sha256: sha256:3239d030e360ede40cd11722bdfb664d19ec96abc6f7947eac8eef27fe6b48e5
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-01-9470-sales-discount-growth
  effective_sizing_eligible: true
  evaluated_at: '2026-05-08T00:00:00+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-9470-fy2026-q1-profit-recovery
  decision_role: sizing_evidence
  evidence_polarity: supports
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: true
  independence_component_id: fy2026-q1-profit-recovery
  approval_rule_id: analyst-event-evidence-reviewed
  source_refs:
  - ref_path: records/_external/gakken/2026-05-08-fy2026-q1-official-ir.md
    content_sha256: sha256:3239d030e360ede40cd11722bdfb664d19ec96abc6f7947eac8eef27fe6b48e5
  recorded_at: '2026-05-08T00:00:00+09:00'
- evidence_hit_id: research-9470-risk-review
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/gakken/2026-05-08-fy2026-q1-official-ir.md
    content_sha256: sha256:3239d030e360ede40cd11722bdfb664d19ec96abc6f7947eac8eef27fe6b48e5
  recorded_at: '2026-05-08T00:00:00+09:00'
independent_evidence_count: 2
raw_playbook_concurrence_count: 1
sizing_eligible_playbook_concurrence_count: 1
raw_evidence_family_count: 2
sizing_eligible_evidence_family_count: 2
conviction_tier: medium
conviction_tier_path: count_breadth
depth_verification_ref: null
position_sizing_overlay:
  paper_proxy_position_size_oku: 0.01
  paper_proxy_position_size_yen: 1000000
  real_order_intent_yen: 196400
  adv_participation_pct: 0.8333
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual: null
thesis_payoff:
  max_entry_price_yen: 982
  target_price_yen: 1100
  stop_loss_yen: 920
  time_horizon_bd: 40
  invalidation_conditions:
  - 2026-05-15 2Q earnings が売上成長または margin recovery を無効化する。
  entry_trigger: post_entry_review_required
  expected_upside_pct: 12.02
  expected_downside_pct: 6.31
  risk_reward_ratio: 1.9
tracking:
  mode: post_approval
  plus_15bd: null
  plus_30bd: null
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
---

# リサーチ: 2026-05-08 9470 学研ホールディングス sales-discount-growth

## Thesis（投資仮説）

ユーザーは 2026-05-01 screening result を見た後に 9470 を買い付けた。現在の records に買付履歴がなかったため、この memo では現在保有している事実と、trade ledger に載せるための research check を記録する。broker position snapshot では 200 株、982 円の保有が確認されている。

candidate fact は明確。9470 は P/S 0.21、P/S sector gap -90.5%、売上 YoY +6.0%、営業黒字で sales-discount-growth に該当した。公式 FY2026 Q1 IR では、売上 +6.0% YoY、EBITDA +38.9%、営業利益 +85.7% が確認でき、2 つ目の evidence component として使える。一方、Q1 の親会社株主帰属利益は減少しており、margin quality も未確認のため、2026-05-15 FY2026 Q2 決算後に必ず確認する。

## Macro regime gate（マクロ・セクターゲート）

- Gate: supportive / pass。
- Sector: 情報・通信業。
- 参照: `records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml`。
- 学研個別の confidence は low。sector view の supportive は AI / cloud spillover が主因だが、学研は教育・医療福祉コンテンツ / サービスの色が強い。

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
