---
title: "Trade runbook"
summary: "Operational entry point for recording trades after accepted research decisions."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/trades.md"
  - "../components/research.md"
---

# Trade runbook

Trade は採用済み research packet に対する執行記録です。自動発注は行いません。contract は [`../components/trades.md`](../components/trades.md) を正本とします。

## Before writing

1. 対応する research が `decision: accepted` であることを確認する。
2. `research_ref` が実在することを確認する。
3. entry price、position size、planned exit、stop loss を記録する。

## After writing

```bash
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```
