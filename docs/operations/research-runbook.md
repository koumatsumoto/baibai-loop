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
4. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-09 を全体 gate として確認する。特に AP-01, AP-02, AP-03, AP-04, AP-06, AP-08, AP-09 は research で重点確認する。

## Rules

- `candidates_ref` と `outlook_ref` を必ず実在 path にする。
- Macro gate を通らない銘柄を採用しない。
- 銘柄固有の事実は、業種を問わず会社IRを一次情報として確認し、出典と計算根拠を残す。直近決算短信、
  決算説明資料、Q&A、有価証券報告書 / 統合報告書、中期経営計画、株主還元関連開示を未確認のまま
  `decision: accepted` にしない。
- 採用判定は `decision: accepted | skipped | pending` の意味を [`../components/research.md`](../components/research.md) に合わせる。
- `decision` を flip する場合 (例: `skipped → accepted`)、`baibai-loop-ledger sync` で paper / skipped 両 ledger に同 ticker の行が並ぶ。これは [`../components/ledger.md §5.1`](../components/ledger.md) の audit log 設計どおりの挙動。current state を見るときは research の最新 `decision` を正本とする。

## After writing

```bash
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```
