---
title: "Outlook runbook"
summary: "Operational entry point for creating or updating records/02-outlook artifacts."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/outlook.md"
  - "../components/brief.md"
---

# Outlook runbook

Outlook は `records/01-brief/` を source として作る macro analysis layer です。contract と self-review は [`../components/outlook.md`](../components/outlook.md) を正本とします。

## Before writing

1. 最新 brief の鮮度を確認する。
2. 発行日 ±5 営業日の FOMC / BOJ / CPI / PCE / NFP / OPEC+ などの release を確認する。
3. outlook に入れる fact がすべて brief から辿れるか確認する。
4. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-08 を全体 gate として確認する。outlook では特に AP-01, AP-06, AP-07 を重点確認する。

## Rules

- outlook 内の fact は `source_refs` で brief YAML を参照する。
- 外部 URL を outlook の正本 source にしない。
- sector / region 判定は [`../components/outlook.md`](../components/outlook.md) の self-review checklist を通す。

## After writing

```bash
uv run baibai-loop-validate
```
