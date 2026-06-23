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
3. Research の `Entry preflight` が `proceed` または `starter` で、未解消 blocker がないことを確認する。`defer` のまま発注しない。`exception` の場合は、例外理由と低 sizing / event / exposure の扱いを trade 本文に要約する。
4. 集中度は 2 つの gate が二段で効く: (a) research preflight `entry_preflight.tactical_exposure_after_order.sector_33_pct > 50` / `playbook_pct > 50` は `proceed` の hard trigger (`tactical_real_budget_yen` 分母)、(b) trade validator は実 capital 分母で sector 45% / playbook 35% / ticker 8% を error として gate する (`position/policy.py` の `max_*_real_concentration_pct`)。pre-trade の手動 sanity check は `baibai-loop-position benchmark` の positions 一覧で行い、新規 entry の sector/playbook 加算は research preflight で確認する。
5. Decision register の `order_intent.order_intent_id` を確認する。
6. 成行・指値・寄成などの注文種別、休場日、次回立会日を確認する。
7. `orders[]` と `executions[]` を分け、broker 側 ID は optional external ID として記録する。
8. 約定済みなら `executions[]` に数量・単価・時刻を記録し、`position_state` を更新する。
9. 未送信、取消、失効、broker reject は `trade_execution_state` と `orders[].state` で表す。

## After writing

```bash
uv run baibai-loop-position sync --root .
uv run baibai-loop-validation --target ledger
uv run baibai-loop-validation
```

ledger sync 後に ledger target を検証し、最後に full validate を通す。`order_intent.order_intent_id` と `orders[].origin_order_intent_id` が join できること、`guarded_max_notional_yen = quantity * order_price_guard_yen` であることを確認する。
