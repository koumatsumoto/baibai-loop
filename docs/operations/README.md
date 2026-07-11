---
title: "Operations index"
summary: "工程横断の運用手順（continuous decision cycle、基盤改善ループ、決算後確認タスク、障害対応）の入口。工程ごとの手順は workflow/ を参照。"
doc_type: operation-index
status: active
last_reviewed: 2026-07-11
---

# Operations

`docs/operations/` は工程横断の運用手順を置く。単一ループの各工程（macro / screening / research / position）の手順は [`../workflow/`](../workflow/) を参照する。

| runbook | 使う場面 |
| --- | --- |
| [`decision-cycle.md`](./decision-cycle.md) | 随時の機会判断、pending order、月次入金、決算・重要event、年次outcomeをtriggerごとに進める e2e 導線 |
| [`improvement-loop.md`](./improvement-loop.md) | 基盤（screening / select / E[r] / マクロ読み）の精度を計測で改善するサイクル |
| [`task-runbook.md`](./task-runbook.md) | 決算後確認など将来イベント後に実行する GitHub issue タスク管理 |
| [`incident-runbook.md`](./incident-runbook.md) | source 取得失敗、validator failure、automation failure |

## 原則

- 投資判断の正本は records / workflow doc に残し、issue には期限・確認項目・更新先を記録する。
- records / schema を変更したら [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) の検証を通す。
