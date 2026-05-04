---
title: "Brief runbook"
summary: "Operational entry point for creating or updating records/01-brief artifacts."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/brief.md"
  - "../reference/data-sources.md"
  - "../workflow.md"
---

# Brief runbook

Brief は `records/01-brief/` に置く fact layer です。詳細 contract は [`../components/brief.md`](../components/brief.md)、既存の節番号付き詳細手順は互換 shim の [`../workflow.md`](../workflow.md) に残しています。

## Before writing

1. `world-weekly` / `world-daily` / `macro-monthly` のどれを作るか決める。
2. [`../workflow.md`](../workflow.md) の「brief 作成前の欠損確認」を実行する。
3. 使用 source は [`../reference/data-sources.md`](../reference/data-sources.md) で Tier と代替経路を確認する。
4. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-08 を全体 gate として確認する。brief では特に AP-01, AP-02, AP-05, AP-06, AP-07 を重点確認する。

## Rules

- brief には解釈、予測、相場観を書かない。
- 数値は source と取得日を持つ。
- Tier 1 取得失敗時の例外運用は [`../reference/data-sources.md`](../reference/data-sources.md) を正本とする。

## After writing

```bash
uv run baibai-loop-validate
```
