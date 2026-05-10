---
title: "Outlook runbook"
summary: "Operational entry point for creating or updating records/03-outlook artifacts."
doc_type: operation
status: active
last_reviewed: 2026-05-10
related_docs:
  - "../components/outlook.md"
  - "../components/brief.md"
---

# Outlook runbook

Outlook は `records/02-brief/` を source として作る macro analysis layer です。contract と self-review は [`../components/outlook.md`](../components/outlook.md) を正本とします。

## Before writing

1. 最新 brief の鮮度を確認する。
2. 発行日 ±5 営業日の FOMC / BOJ / CPI / PCE / NFP / OPEC+ などの release を確認する。
3. outlook に入れる fact がすべて brief から辿れるか確認する。
4. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-08 を全体 gate として確認する。outlook では特に AP-01, AP-06, AP-07 を重点確認する。

## Rules

- outlook 内の fact は `source_refs` で brief YAML を参照する。
- 外部 URL を outlook の正本 source にしない。
- sector / exposure bucket 判定は [`../components/outlook.md`](../components/outlook.md) の self-review checklist を通す。

## After writing

### Third-party verification

Outlook を作成・更新したら、作成済み YAML の `summary` / `sectors` / `exposure_buckets` /
`changes` に含まれる fact を、第三者検証で確認する。ここでの第三者検証とは、**作成済み
outlook が引用している brief を入口に、各数値・日付・固有事実を元 source へ個別に再照会し、
brief の値と outlook の引用値・解釈前提が一致しているかを照合する工程**を指す。

- outlook から外部 URL へ直接飛ばず、まず `source_refs` の brief を開く
- brief の `sources[].url` を再取得し、outlook が使った値・期間・公表日が source 本文と一致するか確認する
- sector / exposure bucket の status を支える fact が `source_refs` に含まれているか確認する
- 照合不能な fact を根拠にした status 変更は行わず、neutral / null 側へ戻す
- spot check では完了扱いにしない。`summary` / `changes` の fact と、neutral 以外の sector / exposure bucket status を全件列挙する
- 証跡は同じ PR の body / comment に `Third-party verification log` として残す。PR にしない作業では完了報告に同じ内容を残す

Verification log には最低限、`target_path`, `verified_at`, `verifier`, `outlook_field_path`,
`source_ref`, `brief_field_path`, `source_url`, `brief_value`, `outlook_value`,
`source_value`, `status`, `action_if_failed` を含める。local draft を `.plan/` に置いてもよいが、
source of truth にはしない。

```bash
uv run baibai-loop-validate
```
