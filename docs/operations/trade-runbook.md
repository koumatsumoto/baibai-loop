---
title: "Trade runbook"
summary: "Operational entry point for recording trades after approved research decisions."
doc_type: operation
status: active
last_reviewed: 2026-05-05
related_docs:
  - "../components/trades.md"
  - "../components/research.md"
---

# Trade runbook

Trade は採用済み research packet に対する執行記録です。自動発注は行いません。contract は [`../components/trades.md`](../components/trades.md) を正本とします。

## Before writing

1. 対応する research が `research_decision.outcome: approved` であることを確認する。
2. `research_ref` が実在することを確認する。
3. Decision register の `order_intent.order_intent_id` を確認する。
4. 成行・指値・寄成などの注文種別、休場日、次回立会日を確認する。
5. `orders[]` と `executions[]` を分け、broker 側 ID は optional external ID として記録する。
6. 約定済みなら `executions[]` に数量・単価・時刻を記録し、`position_state` を更新する。
7. 未送信、取消、失効、broker reject は `trade_execution_state` と `orders[].state` で表す。

## After writing

```bash
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```

validate 後に、`order_intent.order_intent_id` と `orders[].origin_order_intent_id` が join できること、`guarded_max_notional_yen = quantity * order_price_guard_yen` であることを確認する。
