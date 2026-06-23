---
title: "Operations index"
summary: "Runbook entry point for daily cycle, macro context, screening, thesis, position, review, and incidents."
doc_type: operation-index
status: active
last_reviewed: 2026-06-23
---

# Operations

`docs/operations/` は「いつ、どう作るか」の入口です。成果物の schema、必須 field、品質基準は [`../components/README.md`](../components/README.md) を正本とし、runbook には重複して長文を置きません。

| runbook | 使う場面 |
| --- | --- |
| [`screening-runbook.md`](./screening-runbook.md) | candidates 生成、select、research 候補選定 |
| [`thesis-runbook.md`](./thesis-runbook.md) | `records/05-thesis/` を作る前 |
| [`position-runbook.md`](./position-runbook.md) | research 採用後の trade 記録 |
| [`task-runbook.md`](./task-runbook.md) | 決算後確認など将来イベント後に実行する GitHub issue タスク管理 |
| [`incident-runbook.md`](./incident-runbook.md) | source 取得失敗、validator failure、ledger sync failure |
| [`backtest-runbook.md`](./backtest-runbook.md) | 7 axis backtest 手順と過去計測の dated index |

## 原則

- component docs の節番号を壊さない。必要な詳細は component docs へリンクする。
- macro context は screening 前に確認し、必要な場合だけ更新する。
- records / schema を変更したら [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) の検証を通す。
