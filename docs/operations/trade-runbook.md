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
3. 成行・指値・寄成などの注文種別、休場日、次回立会日を確認する。
4. 約定済みなら entry price、position size、planned exit、stop loss を記録する。
5. 注文済みだが未約定なら `status: ordered`、`entry_date: null`、`entry_price: null` で記録し、
   推定約定価格を入れない。約定後に `status: open` へ更新する。

## After writing

```bash
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```
