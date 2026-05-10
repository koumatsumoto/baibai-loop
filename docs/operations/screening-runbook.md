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

Screening は `records/04-candidates/` の fact snapshot を生成し、research 候補選定を支援します。詳細な subsystem 仕様は [`../screening/README.md`](../screening/README.md) から辿ります。

## Generate candidates

```bash
uv run baibai-loop-screening rebuild-cache
uv run baibai-loop-screening verify-cache-coverage --asof YYYY-MM-DD
uv run baibai-loop-screening run --asof YYYY-MM-DD
```

生成物の contract は [`../components/candidates.md`](../components/candidates.md) を参照します。

`run` は開始時に SQLite cache を既存 raw JSON から refresh し、その後 SQLite coverage を検証します。SQLite が `--asof` に必要な J-Quants / JPX 入力を提供できない場合、`run` は fail-fast し、raw JSON や provider API へフォールバックしません。不足が出た場合は raw JSON を補完し、`rebuild-cache` / `verify-cache-coverage` を通してから再実行します。

JPX 規制情報（特別注意 / 整理 / 取引停止 / 上場廃止警告）は universe の必須 gate です。設定された required source が欠ける場合、`run` は fail-fast し、candidates YAML を生成しません。EDINET 前処理済み metrics は任意 source であり、未ロード時は EV/EBITDA などが `unavailable` に degrade します。`EDINET_API_KEY` を設定して EDINET metrics を必須入力として運用する場合は、事前に `verify-cache-coverage --asof YYYY-MM-DD --require-edinet-metrics` を通します。

## Select research candidates

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD
```

`select` は最新 candidates と outlook を組み合わせ、Macro regime gate を支援します。最終採用判断は [`../components/research.md`](../components/research.md) の boundary に従い、人間が確定します。

`select` は `adverse` 業種を除外した上で、lane-specific metric に基づき triage します。Hit 数と時価総額だけで機械的に上位化しません。出力の `candidates` は lane 分散済みの research 着手候補、`ranked_candidates` はグローバル順位、`lane_toplists` は lane 別上位です。`selection_lane`、`selection_metrics` を見て primary thesis を決めます。`recommendation_lane` は lane 分散でその候補を拾った枠で、複数 hit 銘柄では `selection_lane` と異なる場合があります。`candidates` の件数は `--top` と `output.research_selection_target_max`、lane 別一覧の件数は `records/_config/screening-rules/2026-05-01T000000+0900.yaml` の `output.lane_toplist_limit` で調整します。

Lane ごとの hit 数は `evidence_hits_summary` で確認します。特定 lane が universe の大きな割合を占める場合は、候補数が増えただけで evidence の識別力が弱い可能性があるため、`records/_config/screening-rules/2026-05-01T000000+0900.yaml` の閾値・sector policy を見直します。

## After running

```bash
uv run baibai-loop-validate
```
