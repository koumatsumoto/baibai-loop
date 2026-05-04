---
title: "Operations index"
summary: "Runbook entry point for daily cycle, brief, outlook, screening, trade, review, and incidents."
doc_type: operation-index
status: active
last_reviewed: 2026-05-04
---

# Operations

`docs/operations/` は「いつ、どう作るか」の入口です。成果物の schema、必須 field、品質基準は [`../components/README.md`](../components/README.md) を正本とし、runbook には重複して長文を置きません。

| runbook | 使う場面 |
| --- | --- |
| [`daily-cycle.md`](./daily-cycle.md) | 日々の作業入口と freshness 確認 |
| [`brief-runbook.md`](./brief-runbook.md) | `records/01-brief/` を作る前 |
| [`outlook-runbook.md`](./outlook-runbook.md) | `records/02-outlook/` を作る前 |
| [`screening-runbook.md`](./screening-runbook.md) | candidates 生成、select、research 候補選定 |
| [`trade-runbook.md`](./trade-runbook.md) | research 採用後の trade 記録 |
| [`review-runbook.md`](./review-runbook.md) | trade 後 review と monthly retro |
| [`incident-runbook.md`](./incident-runbook.md) | source 取得失敗、validator failure、ledger sync failure |

## 原則

- component docs の節番号を壊さない。必要な詳細は component docs へリンクする。
- fact layer と analysis layer を混ぜない。
- records / schema を変更したら [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) の検証を通す。
