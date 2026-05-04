---
title: "Research runbook"
summary: "Operational entry point for creating records/04-research packets from candidates and outlook."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/research.md"
  - "../components/candidates.md"
  - "../components/outlook.md"
  - "./screening-runbook.md"
---

# Research runbook

Research は `records/03-candidates/` と `records/02-outlook/` を統合する analysis layer です。packet の contract、front matter、AI 境界、self-review は [`../components/research.md`](../components/research.md) を正本とします。

## Before writing

1. 最新 candidates と最新 outlook が存在することを確認する。
2. Macro gate は [`screening-runbook.md`](./screening-runbook.md) と [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md) に従う。
3. 使用する playbook が [`../components/playbooks.md`](../components/playbooks.md) と `records/_playbooks/` から辿れることを確認する。
4. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-08 を全体 gate として確認する。特に AP-01, AP-02, AP-03, AP-04, AP-06, AP-08 は research で重点確認する。

## Rules

- `candidates_ref` と `outlook_ref` を必ず実在 path にする。
- Macro gate を通らない銘柄を採用しない。
- 銘柄固有の事実は一次情報で確認し、出典と計算根拠を残す。
- 採用判定は `decision: accepted | skipped | pending` の意味を [`../components/research.md`](../components/research.md) に合わせる。

## After writing

```bash
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```
