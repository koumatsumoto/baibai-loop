---
ticker: '9682'
name: ＤＴＳ
playbook_id: sales-discount-growth
playbook_ref:
  ref_path: records/_playbooks/sales-discount-growth/2026-05-01T000000+0900.md
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
  evidence_hit_id: candidate-2026-05-01-9682-sales-discount-growth
research_decision:
  outcome: approved
  posture: act_now
  reason_code: pre_refactor_history_restored_and_payoff_pass
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
  screen_run_id: screening-20260501
  ticker: '9682'
  candidate_id: candidate-2026-05-01-9682
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: false
published_at: '2026-05-05T13:31:15+09:00'
recorded_at: '2026-05-05T13:31:15+09:00'
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
    confidence: medium
policy_overrides: []
external_refs:
- ref_path: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
candidate_evidence_decisions:
- evidence_hit_id: candidate-2026-05-01-9682-sales-discount-growth
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T13:31:15+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-9682-shareholder-return
  decision_role: sizing_evidence
  evidence_polarity: supports
  evidence_family_set:
  - catalyst
  source_status: ok
  analyst_asserted: true
  sizing_eligible: true
  independence_component_id: shareholder-return
  approval_rule_id: analyst-event-evidence-reviewed
  source_refs:
  - ref_path: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
  recorded_at: '2026-05-05T13:31:15+09:00'
- evidence_hit_id: research-9682-risk-review
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
  recorded_at: '2026-05-05T13:31:15+09:00'
independent_evidence_count: 2
raw_playbook_concurrence_count: 1
sizing_eligible_playbook_concurrence_count: 1
raw_evidence_family_count: 3
sizing_eligible_evidence_family_count: 3
conviction_tier: medium
conviction_tier_path: count_breadth
depth_verification_ref: null
position_sizing_overlay:
  paper_proxy_position_size_oku: 0.01
  paper_proxy_position_size_yen: 1000000
  real_order_intent_yen: 210000
  adv_participation_pct: 0.2632
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual: null
thesis_payoff:
  max_entry_price_yen: 1050
  target_price_yen: 1230
  stop_loss_yen: 950
  time_horizon_bd: 40
  invalidation_conditions:
  - 株主還元 catalyst が織り込まれた後に株価が 950 円を下回る。
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 17.14
  expected_downside_pct: 9.52
  risk_reward_ratio: 1.8
tracking:
  mode: post_approval
  plus_15bd: null
  plus_30bd: null
market_cap_oku: 1663
sector_33: 情報・通信業
avg_turnover_oku: 3.8
valuation:
  per_forward: null
  per_trailing: 13.9
  pbr: 2.54
  ev_ebitda: null
  p_s: 1.23
  pcfr: 18.6
  ocf_yield: 0.0537
  fcf_yield: 0.0274
  net_cash_to_market_cap: null
  cash_to_market_cap: 0.1767
  price_to_equity: 2.5699
  equity_ratio: 0.7589
  primary_metric:
  - p_s
---

# リサーチ: 2026-05-05 9682 DTS sales-discount-growth

## Thesis（投資仮説）

DTS は、リファクタリング前に承認済みだったメモを現在の records に戻したもの。過去の git history では、9682 は 2026-05-07 より前にリサーチされ、注文対象になっていた。現在の broker position snapshot では、200 株が 1,010 円で約定している。

仮説は狭い。P/S 1.23、売上 YoY +7.4%、営業黒字、株主還元を根拠にした小さめの情報サービス銘柄トレードであり、高 conviction の rerating 狙いではない。sector gate は supportive だが、ポジションサイズは抑える。

## Macro regime gate（マクロ・セクターゲート）

- Gate: supportive / pass。
- Sector: 情報・通信業。
- 参照: `records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml`。
- セクターゲートは初期ポジションを許容する。ただし DTS は伝統的な SIer 色が強いため、AI / cloud の波及効果を過大評価しない。

## Sales / P/S snapshot（売上・P/S）

| 指標 | 値 |
| --- | ---: |
| P/S | 1.23 |
| P/S sector gap | -45.5% |
| Sales TTM | 135,213 百万円 |
| Sales YoY | +7.4% |
| 営業利益 | 16,434 百万円 |
| OCF yield | 5.37% |
| FCF yield | 2.74% |

2026-05-01 candidate row を canonical fact source とする。

## Margin bridge（利益率の見立て）

candidate data では売上成長と営業黒字が確認できる。sales-discount-growth の条件は満たすが、P/S discount は SIer 事業の低 multiple を反映している可能性もあるため、ポジションは小さく保つ。

## CFO / loss narrowing（CFO・赤字縮小）

DTS は営業黒字のため、赤字縮小は gate 条件ではない。OCF yield はプラスだが、cash-flow discount thesis と言えるほど強い根拠ではない。

## Growth durability（成長持続性）

成長持続性は、SI / DX 需要の継続と project margin の悪化がないことに依存する。次回四半期開示で margin pressure や受注品質の弱さが見えた場合は再評価する。

## Shareholder return（株主還元）

過去リサーチでは、増配と自己株買い / 消却を追加 catalyst として扱っていた。この材料は analyst-confirmed の sizing eligible evidence とし、元の外部確認ログは `records/_external/` 配下に保存されている。

## Entry（エントリー）

2026-05-07 に 200 株、1,010 円で約定。旧 order guard は 1,050 円で、実際の約定価格は guard を下回った。

## Exit（出口）

目標株価は 1,230 円。損切り価格は 950 円。仮説が先に invalidation されない限り、time stop は 40 営業日。

## Invalidation（無効化条件）

- 株主還元 news が織り込まれた後、株価が 950 円を下回る。
- SIer peer comparison で P/S discount に意味がないと分かる。
- Sector gate が supportive から neutral / adverse に悪化する。

## Position size（ポジションサイズ）

- 数量: 200 株。
- 判断時の guarded notional: 210,000 円。
- 実際の entry notional: 202,000 円。
- 実資金 5,000,000 円に対する concentration: 4.04%。
- tactical budget 1,000,000 円に対する concentration: 20.20%。
- この record は実約定済みポジションを現在の records に戻すもの。月次 retro では、リファクタリング前 record の復元ケースとして確認する。
