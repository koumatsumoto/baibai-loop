---
title: "Screening runbook"
summary: "Operational entry point for candidates generation, selection, and research handoff."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/candidates.md"
  - "../screening/README.md"
  - "../components/research.md"
---

# Screening runbook

Screening は `records/03-candidates/` の fact snapshot を生成し、research 候補選定を支援します。詳細な subsystem 仕様は [`../screening/README.md`](../screening/README.md) から辿ります。

## Generate candidates

```bash
uv run baibai-loop-screening run --asof YYYY-MM-DD
```

生成物の contract は [`../components/candidates.md`](../components/candidates.md) を参照します。

## Select research candidates

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD
```

`select` は最新 candidates と outlook を組み合わせ、Macro gate を支援します。最終採用判断は [`../components/research.md`](../components/research.md) の boundary に従い、人間が確定します。

## After running

```bash
uv run baibai-loop-validate
```
