---
ticker: "XXXX"
name: "..."
playbook_id: valuation-reversion
playbook_ref:
  ref_path: records/_playbooks/valuation-reversion/YYYY-MM-DDTHHMMSS+0900.md
  effective_from: "YYYY-MM-DDTHH:MM:SS+09:00"
candidate_ref:
  candidates_ref: records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml
  ticker: "XXXX"
research_decision:
  outcome: approved | deferred | rejected
  posture: act_now | wait_for_event | wait_for_capital | dropped
  rejection_reason: thesis_failed | corporate_action_post_snapshot | source_stale | policy_block | other
  deferral_reason: data_gap | event_pending | macro_context_caution | capital_constraint | other
macro_context_ref: records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml
macro_context_fit:
  context_freshness: current | stale | future
  fit: tailwind | neutral | mixed | headwind | not_matched
  decision_effect: proceed | caution | defer
  required_checks: []
  sizing_caution: []
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
corporate_action_check:
  checked: true
  result: none
  note: ""
entry_preflight:
  evaluated_on: "YYYY-MM-DD"
  market_relative_return_pct: 0.0
  sector_or_peer_relative_return_pct: 0.0
  macro_freshness: current
  market_regime:
    regime: risk_on_rally | risk_off_selloff | neutral_range | unknown
    benchmark_return_20d: 0.0
    benchmark_ticker: "1321"
    asof: "YYYY-MM-DD"
    eval_date: "YYYY-MM-DD"
  tactical_exposure_after_order:
    sector_33_pct: 0.0
    playbook_pct: 0.0
  near_term_catalyst: false
  action: proceed
  reason: "[relative return / macro freshness / exposure / market regime を踏まえた entry 判断]"
sector_33: "情報・通信業"
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
---

# Research: YYYY-MM-DD XXXX [銘柄名] [playbook_id]

**成分**: Decision lifecycle の **research / investment memo**（[`/docs/components/research.md`](/docs/components/research.md)）

## 1. Thesis

Why now × why this stock。主要 evidence、payoff、反対仮説、macro context fit を一文で結論づける。

- **Swing thesis**: [5-40 営業日で価格回復・catalyst・需給改善により利確できる理由。]
- **Long-hold fallback**: [短期 thesis が外れた場合でも、長期保有になっても耐えられる可能性が高い balance sheet / cash flow / liquidity / refinancing risk / earnings base の耐久性があり、資産ロックを受け入れて長期保有へ切り替えられるか。固定年数ではなく、売却までの期間が想定より長引いても事業継続性と回収余地が残るか。stop loss / invalidation / kill switch / 事業継続前提の毀損を上書きしないか。]
- **Capital lock / shareholder return**: [含み損時の資産ロックを受け入れる前提で、配当・自己株買い・安定 shareholder return があるか。配当がない場合は、短期リターン可能性と payoff が十分大きいか。]
- **AI long-term impact**: [AI の長期機会、長期脅威、今回判断での重み。AI 期待は単独の採用根拠・sizing 根拠にしない。]

## 2. Macro Context

- **macro_context_ref**: [records/01-macro-context/...]
- **context_freshness**: current | stale | future
- **fit**: tailwind | neutral | mixed | headwind | not_matched
- **decision_effect**: proceed | caution | defer
- **required_checks**: [深掘りが必要な前提]
- **sizing_caution**: [必要なら低 sizing cap の理由]

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

### Entry preflight

| Check | Value | Source / note |
| --- | --- | --- |
| Price window | start: YYYY-MM-DD / entry check: YYYY-MM-DD / basis: close_unadjusted | |
| Market baseline | Nikkei or TOPIX return: X.X% | |
| Sector / peer baseline | sector index / peer basket / not_checked: X.X% | not_checked の場合は理由を書く |
| Relative return | vs market: X.Xpt / vs sector-peer: X.Xpt | |
| Macro freshness | current / stale / future | stale / future の扱いを書く |
| Exposure review | after-order sector: X.X% / playbook: X.X% | 分母は `tactical_real_budget_yen`。分子は追加予定 order を含めた同一 sector / playbook の open entry / guarded notional |
| Action | proceed / starter / defer / exception | 3pt 以上劣後、stale macro、50% 超 exposure の扱いを書く |

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

- **paper proxy size**:
- **estimated real order notional**:
- **binding cap**:

参照: [`/docs/components/research.md`](/docs/components/research.md), [`/docs/screening/principles.md`](/docs/screening/principles.md), [`/records/_playbooks/`](/records/_playbooks/)
