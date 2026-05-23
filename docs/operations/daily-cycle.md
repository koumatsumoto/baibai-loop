---
title: "Daily cycle runbook"
summary: "Daily entry point for freshness checks, macro updates, screening, and validation."
doc_type: operation
status: active
last_reviewed: 2026-05-04
---

# Daily cycle runbook

## Start

1. 直近の作業対象を決める: macro context、screening、research、trade、review。
2. [`../components/README.md`](../components/README.md) で対象 component の contract を開く。
3. records を変更する場合は、変更前に該当 runbook と [`../anti-patterns.md`](../anti-patterns.md) の relevant checklist を読む。

## Freshness

- Macro context は screening 手前で確認し、主要イベントや相場急変があれば更新する。

## Validation

records や schema を触った日は、最低限以下を実行します。

```bash
uv run baibai-loop-validate
```

PR 前の full verification は [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) を参照します。
