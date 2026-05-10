---
ticker: "XXXX"
name: "..."
playbook_id: valuation-reversion
playbook_snapshot:
  ref_path: records/_playbooks/valuation-reversion/YYYY-MM-DDTHHMMSS+0900.md
  content_sha256: sha256:<64-hex>
  effective_from: "YYYY-MM-DDTHH:MM:SS+09:00"
policy_snapshot:
  ref_path: records/01-policy/YYYY/MM/YYYY-MM-DDTHHMMSS+0900-portfolio-policy.md
  content_sha256: sha256:<64-hex>
  effective_from: "YYYY-MM-DDTHH:MM:SS+09:00"
portfolio_exposure_snapshot_ref:
  ref_path: records/_portfolio-exposure/YYYY/MM/YYYY-MM-DDTHHMMSS+0900.yaml
  content_sha256: sha256:<64-hex>
  as_of: "YYYY-MM-DDTHH:MM:SS+09:00"
candidate_ref:
  candidates_ref: records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml
  screen_run_id: screening-YYYYMMDD
  ticker: "XXXX"
  candidate_id: candidate-YYYY-MM-DD-XXXX
research_decision:
  outcome: approved | deferred | rejected
  posture: act_now | wait_for_event | wait_for_capital | dropped
  rejection_reason: thesis_failed | corporate_action_post_snapshot | source_stale | policy_block | other
  deferral_reason: data_gap | event_pending | regime_block | capital_constraint | other
macro_regime_gate:
  aggregate_status: supportive | neutral | adverse | unknown
  decision_effect: pass | conditional | block
  position_cap_reason: null
candidate_evidence_decisions:
  - evidence_hit_id: eh-XXXX
    effective_sizing_eligible: true
    evaluated_at: "YYYY-MM-DDTHH:MM:SS+09:00"
    reason_code: source_status_ok | freshness_expired | corporate_action_post_snapshot | duplicate_dependency | other
selected_supporting_evidence_refs:
  - source: candidate
    evidence_hit_id: eh-XXXX
research_evidence_hits:
  - evidence_hit_id: risk-XXXX
    decision_role: risk_evidence
    evidence_polarity: risk
    source_status: ok
independent_evidence_count: 1
raw_evidence_family_count: 1
sizing_eligible_evidence_family_count: 1
raw_playbook_concurrence_count: 1
sizing_eligible_playbook_concurrence_count: 1
conviction_tier: low | medium | high | blocked
conviction_tier_path: count_breadth | depth
position_sizing_overlay:
  paper_proxy_position_size_yen: 1000000
  real_order_intent_yen: 210000
  adv_participation_pct: 0.5
thesis_payoff:
  max_entry_price_yen: 1000
  target_price_yen: 1300
  stop_loss_yen: 900
  expected_upside_pct: 30.0
  expected_downside_pct: 10.0
  risk_reward_ratio: 3.0
  time_horizon_bd: 30
  invalidation_conditions:
    - stop loss
sector_33: "情報・通信業"
ai-draft: true
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
external_refs: []
---

# Research: YYYY-MM-DD XXXX [銘柄名] [playbook_id]

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/research.md`](/docs/components/research.md)）

## 1. Thesis

Why now × why this stock。主要 evidence、payoff、反対仮説、macro regime gate を一文で結論づける。

## 2. Macro regime gate

- **aggregate_status**: supportive | neutral | adverse | unknown
- **decision_effect**: pass | conditional | block
- **対象 exposure**: sector / exposure bucket / security exposure
- **outlook_ref**: [outlook path]
- **portfolio policy cap**: [必要なら低 sizing cap の理由]

## 3. Valuation snapshot

| 指標 | 値 | 業種中央値 | 業種中央値比 | source |
| --- | ---: | ---: | ---: | --- |
| PER | | | | |
| PBR | | | | |
| P/S | | | | |
| FCF yield | | | | |

## 4. 一時的割安の原因仮説

[一時的である根拠。構造悪化なら rejected / deferred に倒す。]

## 5. 反対仮説

[最低 1 件の risk / contradicting evidence review を記録する。]

## 6. Catalyst

[決算修正、自社株買い、M&A、事業イベント、イベント不在時の margin of safety。]

## 7. Price reaction

[価格、出来高、相対リターン、セクター相対の反応。]

## 8. Positioning / liquidity

[空売り、信用、流動性、board lot、ADV、注文可能性。]

## 9. Shareholder return

[配当、自社株買い、DOE、還元余地、減配リスク。]

## 10. Entry 条件

- **max entry price**:
- **guard price**:
- **quantity / board lot**:
- **not submitted 条件**:

## 11. Exit 条件

- **target**:
- **stop loss**:
- **time stop**:

## 12. Invalidation

[thesis が壊れる条件。]

## 13. Position size

- **conviction_tier**:
- **conviction_tier_path**:
- **paper proxy size**:
- **estimated real order notional**:
- **binding cap**:
- **portfolio exposure snapshot**:

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/`](/records/_playbooks/)
