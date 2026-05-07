---
title: "Daily cycle runbook"
summary: "Daily entry point for freshness checks, macro updates, screening, and validation."
doc_type: operation
status: active
last_reviewed: 2026-05-04
---

# Daily cycle runbook

## Start

1. 直近の作業対象を決める: brief freshness、outlook 更新、screening、research、trade、review。
2. [`../components/README.md`](../components/README.md) で対象 component の contract を開く。
3. records を変更する場合は、変更前に該当 runbook と [`../anti-patterns.md`](../anti-patterns.md) の relevant checklist を読む。

## Freshness

- Macro track は売買イベントと独立に確認する。
- `records/02-brief/` の鮮度不足があれば [`brief-runbook.md`](./brief-runbook.md) を優先する。
- 最新 outlook が古い、または重大 event が出た場合は [`outlook-runbook.md`](./outlook-runbook.md) に進む。

## Validation

records や schema を触った日は、最低限以下を実行します。

```bash
uv run baibai-loop-validate
```

PR 前の full verification は [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) を参照します。
