---
title: "Brief runbook"
summary: "Operational entry point for creating or updating records/02-brief artifacts."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/brief.md"
  - "../reference/data-sources.md"
---

# Brief runbook

Brief は `records/02-brief/` に置く fact layer です。詳細 contract は [`../components/brief.md`](../components/brief.md) を正本とします。

## Before writing

1. `world-weekly` / `world-daily` / `macro-monthly` のどれを作るか決める。
2. 「brief 作成前の欠損確認」を実行する。
3. 使用 source は [`../reference/data-sources.md`](../reference/data-sources.md) で Tier と代替経路を確認する。
4. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-08 を全体 gate として確認する。brief では特に AP-01, AP-02, AP-05, AP-06, AP-07 を重点確認する。

## Brief 作成前の欠損確認

- `world-weekly` の対象期間に gap がある場合、現在週を作る前に欠損週を backfill する。
- `world-daily` が存在しても `world-weekly` 欠損の代替にはしない。
- Backfill 後、現在週の `references.prev_period` と前週比計算の基準を更新する。
- 欠損を放置したまま commit / PR しない。

## Rules

- brief には解釈、予測、相場観を書かない。
- 数値は source と取得日を持つ。
- Tier 1 取得失敗時の例外運用は [`../reference/data-sources.md`](../reference/data-sources.md) を正本とする。
- routine な単一統計公表は `macro-monthly` 未作成期間なら `world-daily` に置き、decisive event のときだけ event kind を使う。
- 同じイベントを複数 kind で重複記録しない。週次 brief では該当 kind への link で代替する。

## After writing

```bash
uv run baibai-loop-validate
```
