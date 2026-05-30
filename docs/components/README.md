---
title: "Component contracts"
summary: "Index of Baibai-Loop artifact contracts."
doc_type: component-index
status: active
last_reviewed: 2026-05-30
---

# Components

`docs/components/` は `records/` に残る成果物の contract を扱います。いつ作るか、どの順番で作るかは [`../operations/README.md`](../operations/README.md) に置きます。投資判断プロセス全体の概念モデルは [`../concepts.md`](../concepts.md) を正本とします。

Portfolio policy は record ではなく docs-managed governance document です。Component 境界は [`portfolio-policy.md`](./portfolio-policy.md) で説明します。

| component | repository location | contract doc |
| --- | --- | --- |
| portfolio policy | [`../portfolio-policy.md`](../portfolio-policy.md) | [`portfolio-policy.md`](./portfolio-policy.md) |
| macro context | `records/01-macro-context/` | [`macro-context.md`](./macro-context.md) |
| candidates | `records/04-candidates/` | [`candidates.md`](./candidates.md) |
| research | `records/05-research/` | [`research.md`](./research.md) |
| trades | `records/06-trades/` | [`trades.md`](./trades.md) |
| reviews | `records/07-reviews/` | [`reviews.md`](./reviews.md) |
| ledger | `records/_ledger/` | [`ledger.md`](./ledger.md) |
| playbooks | `records/_playbooks/` | [`playbooks.md`](./playbooks.md) |

## 読む順番

1. [`../architecture/system-overview.md`](../architecture/system-overview.md) で全体構造を確認する。
2. 編集する records path に対応する component doc を読む。
3. 作成手順は [`../operations/README.md`](../operations/README.md) から該当 runbook を読む。
4. template が必要な場合は [`../templates/README.md`](../templates/README.md) を読む。
