---
trade_id: trade-YYYYMMDD-XXXX
ticker: "XXXX"
name: "..."
research_ref: records/05-research/YYYY/MM/YYYY-MM-DD-XXXX-<playbook_id>.md
policy_ref:
  ref_path: records/01-policy/YYYY/MM/YYYY-MM-DDTHHMMSS+0900-portfolio-policy.md
policy_applicability: active
calendar_refs:
  business_days:
    ref_path: records/_calendars/business-days/YYYY-MM.yaml
  events:
    ref_path: records/_calendars/events/YYYY-MM.yaml
  corporate_actions:
    ref_path: records/_calendars/corporate-actions/YYYY-MM.yaml
position_state: none | open | closed
review_state: not_due | scheduled | completed
trade_execution_state: none | submitted | broker_rejected | cancelled | expired | not_filled | partially_filled | filled
order_intent:
  order_intent_id: intent-YYYYMMDD-XXXX-entry
  decision_event_id: decision-YYYYMMDD-XXXX-research
  side: buy | sell
  quantity: 100
  order_price_guard_yen: 1000
  uses_margin: false
  not_submitted_reason: null
position_sizing_overlay:
  estimated_real_order_notional_yen: 100000
  guarded_max_notional_yen: 100000
orders:
  - order_id: order-YYYYMMDD-XXXX-entry
    origin_order_intent_id: intent-YYYYMMDD-XXXX-entry
    external_broker_order_id: null
    side: buy
    state: submitted | broker_rejected | cancelled | expired | not_filled | partially_filled | filled
    submitted_quantity: 100
    filled_quantity: 0
    events:
      - event_type: submit
        at: "YYYY-MM-DDTHH:MM:SS+09:00"
executions: []
planned_exit:
  target_price_yen: 1300
  stop_loss_yen: 900
  time_stop_at: "YYYY-MM-DD"
kill_switch_check:
  earnings_straddle: false
  boj_eve: false
  fomc_eve: false
---

# Trade: YYYY-MM-DD XXXX [銘柄名]

**成分**: Decision lifecycle の **trades / execution record**（[`/docs/components/trades.md`](/docs/components/trades.md)）

**Research source**: [records/05-research/YYYY/MM/YYYY-MM-DD-*-*.md](...)

## 1. Order / Entry

### 1.1 Entry reason

- **Thesis**:
- **Playbook**:
- **Macro regime gate**:
- **Primary valuation metric**:

### 1.2 Order intent

- **order_intent_id**:
- **quantity**:
- **order_price_guard_yen**:
- **guarded_max_notional_yen**:
- **binding cap**:

### 1.3 Orders

| order_id | side | state | submitted | filled | broker / reason |
| --- | --- | --- | ---: | ---: | --- |
| order-... | buy | submitted | 100 | 0 | |

### 1.4 Executions

| execution_id | order_id | side | quantity | price | at |
| --- | --- | --- | ---: | ---: | --- |
| exec-... | order-... | buy | 100 | 1000 | YYYY-MM-DD HH:MM |

## 2. 保有中ログ

[重大な変化、macro regime gate、決算、positioning / liquidity、価格、無効化条件の監視。]

## 3. Exit

### 3.1 Exit reason

[利確 / 損切り / time stop / invalidation / kill switch reversal]

### 3.2 Exit executions

| execution_id | order_id | side | quantity | price | at |
| --- | --- | --- | ---: | ---: | --- |

### 3.3 P&L

- **gross_return_pct**:
- **net_return_pct**:
- **execution_costs_yen**:

## 4. Review への接続

- +15 営業日 review 予定日:
- +30 営業日 review 予定日:
- attribution review: [`records/07-reviews/YYYY/MM/YYYY-MM-DD-XXXX.md`](/records/07-reviews/YYYY/MM/YYYY-MM-DD-XXXX.md)
