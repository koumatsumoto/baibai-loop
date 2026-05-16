---
ticker: '9692'
name: シーイーシー
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
  evidence_hit_id: candidate-2026-05-01-9692-sales-discount-growth
research_decision:
  outcome: approved
  posture: act_now
  reason_code: pre_refactor_history_restored_and_payoff_pass
candidate_ref:
  candidates_ref: records/04-candidates/2026/05/2026-05-01.yaml
  screen_run_id: screening-20260501
  ticker: '9692'
  candidate_id: candidate-2026-05-01-9692
outlook_ref: records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml
brief_refs:
- records/02-brief/2026/05/2026-05-03-world-weekly-fomc-boj-hold.yaml
- records/02-brief/2026/05/2026-05-04-world-daily-us-pce-cn-trade-hormuz.yaml
ai_draft: false
published_at: '2026-05-05T20:05:00+09:00'
recorded_at: '2026-05-05T20:05:00+09:00'
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
- evidence_hit_id: candidate-2026-05-01-9692-sales-discount-growth
  effective_sizing_eligible: true
  evaluated_at: '2026-05-05T20:05:00+09:00'
  reason_code: source_status_ok
research_evidence_hits:
- evidence_hit_id: research-9692-shareholder-return
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
  recorded_at: '2026-05-05T20:05:00+09:00'
- evidence_hit_id: research-9692-risk-review
  decision_role: risk_evidence
  evidence_polarity: risk
  evidence_family_set:
  - fundamental
  source_status: ok
  analyst_asserted: true
  sizing_eligible: false
  source_refs:
  - ref_path: records/_external/deepresearch/2026-05-05-japan-market-reopen-risk.md
  recorded_at: '2026-05-05T20:05:00+09:00'
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
  real_order_intent_yen: 200000
  adv_participation_pct: 0.7692
  sizing_formula_id: policy-v1-paper-to-real-ladder
counterfactual: null
thesis_payoff:
  max_entry_price_yen: 2000
  target_price_yen: 2300
  stop_loss_yen: 1800
  time_horizon_bd: 40
  invalidation_conditions:
  - 成長鈍化が P/S と配当支えを上回る。
  entry_trigger: price_guard_or_revisit
  expected_upside_pct: 15.0
  expected_downside_pct: 10.0
  risk_reward_ratio: 1.5
tracking:
  mode: post_approval
  plus_15bd: null
  plus_30bd: null
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
---

# リサーチ: 2026-05-05 9692 CEC sales-discount-growth

## Thesis（投資仮説）

CEC は、リファクタリング前に承認済みだったメモを現在の records に戻したもの。旧 records では DTS と並んで選定されており、現在の broker position snapshot では 100 株が 1,953 円で約定している。

2026-05-01 candidate snapshot 上では、DTS より仮説は強い。P/S 1.03、売上 YoY +17.2%、OCF yield 8.59%、net cash / market cap 36.61%、配当支えがある。ただし単一銘柄としては控えめなサイズに留め、tactical allocation を大きく張る位置づけではない。

## Macro regime gate（マクロ・セクターゲート）

- Gate: supportive / pass。
- Sector: 情報・通信業。
- 参照: `records/03-outlook/2026/05/outlook-2026-05-04-post-fomc-boj-hold.yaml`。
- CEC は DX / cloud / security spending の恩恵を受けるが、direct AI beta は過大評価しない。

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

配当支えと過去の自己株式取得は、sizing eligible の catalyst evidence として扱う。raw external verification log は `records/_external/` 配下に保存されている。

## Entry（エントリー）

2026-05-07 に 100 株、1,953 円で約定。旧 order guard は 2,000 円で、実際の約定価格は guard を下回った。

## Exit（出口）

目標株価は 2,300 円。損切り価格は 1,800 円。time stop は 40 営業日。2026-06-11 の 1Q window 前に別途 review する。

## Invalidation（無効化条件）

- 売上成長が鈍化し、P/S discount が正当化される。
- OCF が弱まり、配当支えの信頼度が下がる。
- Sector gate が supportive から neutral / adverse に悪化する。

## Position size（ポジションサイズ）

- 数量: 100 株。
- 判断時の guarded notional: 200,000 円。
- 実際の entry notional: 195,300 円。
- 実資金 5,000,000 円に対する concentration: 3.91%。
- tactical budget 1,000,000 円に対する concentration: 19.53%。
- この record は実約定済みポジションを現在の records に戻すもの。月次 retro では、リファクタリング前 record の復元ケースとして確認する。
