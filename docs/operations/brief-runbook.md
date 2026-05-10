---
title: "Brief runbook"
summary: "Operational entry point for creating or updating records/02-brief artifacts."
doc_type: operation
status: active
last_reviewed: 2026-05-10
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

### Third-party verification

Brief を作成・更新したら、作成済み YAML の内容をそのまま信用せず、第三者検証を行う。
ここでの第三者検証とは、**作成済みドキュメントに記載された各数値・日付・固有事実を個別に元
source へ再照会し、値・期間・公表日・source status が正しいかを照合する工程**を指す。

- `sources[].url` を再取得し、本文または公式 schedule に該当値が存在することを確認する
- `layers` / `fact_memos` / `next_events` の値を 1 件ずつ source と突き合わせる
- 前週比・前月比・bp / % / 円換算は機械計算で再検算する
- 照合できない値は `fetch_status: failed` / `status: partial|failed` へ落とし、brief の事実値としては使わない
- spot check では完了扱いにしない。値を持つ field path は全件列挙し、対象外にした field があれば理由を残す
- 証跡は同じ PR の body / comment に `Third-party verification log` として残す。PR にしない作業では完了報告に同じ内容を残す

Verification log には最低限、`target_path`, `verified_at`, `verifier`, `field_path`,
`source_id`, `source_url`, `document_value`, `source_value`, `calculation_check`,
`status`, `action_if_failed` を含める。local draft を `.plan/` に置いてもよいが、
source of truth にはしない。

```bash
uv run baibai-loop-validate
```
