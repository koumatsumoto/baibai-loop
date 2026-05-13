---
title: "Screening runbook"
summary: "Operational entry point for candidates generation, selection, and research handoff."
doc_type: operation
status: active
last_reviewed: 2026-05-13
related_docs:
  - "../components/candidates.md"
  - "../screening/README.md"
  - "../components/research.md"
---

# Screening runbook

Screening は `records/04-candidates/` の fact snapshot を生成し、research 候補選定を支援します。詳細な subsystem 仕様は [`../screening/README.md`](../screening/README.md) から辿ります。

## Generate candidates

```bash
uv run baibai-loop-screening bootstrap-cache --asof YYYY-MM-DD
uv run baibai-loop-screening extract-edinet-metrics --asof YYYY-MM-DD --lookback-days 540
uv run baibai-loop-screening verify-cache-coverage --asof YYYY-MM-DD
uv run baibai-loop-screening run --asof YYYY-MM-DD
```

生成物の contract は [`../components/candidates.md`](../components/candidates.md) を参照します。

`run` は開始時に SQLite coverage を検証します。SQLite が `--asof` に必要な J-Quants / JPX / EDINET metrics 入力を提供できない場合、`run` は fail-fast し、raw JSON や provider API へフォールバックしません。coverage 検証は read-only で、schema migration は行いません。master の common-stock universe、日次足の長期履歴密度、財務サマリーの ticker coverage も確認します。不足が出た場合は `bootstrap-cache --asof` または `extract-edinet-metrics` で SQLite を補完し、`verify-cache-coverage` を通してから再実行します。

JPX 規制情報（特別注意 / 整理 / 取引停止 / 上場廃止警告）は universe の必須 gate です。設定された required source が欠ける場合、`run` は fail-fast し、candidates YAML を生成しません。EDINET 前処理済み metrics も `run` の必須 coverage です。`EDINET_API_KEY` は `extract-edinet-metrics` 実行時だけ必要で、`run` 中に EDINET API へフォールバックしません。


## Select research candidates

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD
```

`select` は最新 candidates と outlook を組み合わせ、Macro regime gate を支援します。最終採用判断は [`../components/research.md`](../components/research.md) の boundary に従い、人間が確定します。

`select` は `adverse` 業種を除外した上で、lane-specific metric、短期 dislocation、long-hold survivability、過去 research decision を組み合わせて triage します。Hit 数と時価総額だけで機械的に上位化しません。

出力は `queues` と `selection.diagnostics` を正本にします。

- `queues.recommended_research_queue`: research 着手候補。旧 top-level `candidates` はこの alias
- `queues.fast_dislocation_queue`: 1d / 5d / 20d / 60d の下落、52 週安値距離、出来高変化のいずれかと、OCF / FCF / net cash / equity buffer / sales+profit の fundamental guard を同時に満たす候補
- `queues.core_value_queue`: 既存 playbook lane の分散候補
- `queues.long_hold_survivability_queue`: 長期保有耐性が `high|medium` の候補
- `queues.deferred_revisit_queue` / `queues.suppressed_queue`: ledger 上の deferred / rejected により通常 recommendation から外した候補

`selection_lane`、`selection_metrics` を見て primary thesis を決めます。`recommendation_lane` はその候補を拾った queue / lane で、複数 hit 銘柄では `selection_lane` と異なる場合があります。件数は `--top` と `output.research_selection_target_max`、lane 別一覧の件数は `records/_config/screening-rules/2026-05-01T000000+0900.yaml` の `output.lane_toplist_limit` で調整します。

Parameter replay は `select-sweep` で行います。

```bash
uv run baibai-loop-screening select-sweep --asof YYYY-MM-DD --profiles strict,balanced,loose
```

閾値を試す場合は source code を編集せず、YAML profile を渡します。

```bash
uv run baibai-loop-screening select-sweep \
  --asof YYYY-MM-DD \
  --profile-config path/to/selection-profiles.yaml \
  --profiles strict,balanced,my-fast-lane
```

`select-sweep` では profile ごとの recommended tickers、fast-dislocation tickers、long-hold counts、suppressed count、previous overlap、sector / lane concentration を比較します。運用設定を変える前に、複数 asof の実データで `strict` / `balanced` / `loose` と候補 profile を比較します。

サイロ化を避けるため、既定 profile は `selection.diversity.max_previous_candidates_in_recommended` で前回 candidates 由来の銘柄数に上限を置きます。上限に達した場合、同じ queue 内で新規候補を優先して埋めます。ただし候補が足りず `research_selection_target_min` を下回る場合は、最低件数を満たすために diversity を緩めます。

Lane ごとの hit 数は `evidence_hits_summary` で確認します。特定 lane が universe の大きな割合を占める場合は、候補数が増えただけで evidence の識別力が弱い可能性があるため、`records/_config/screening-rules/2026-05-01T000000+0900.yaml` の閾値・sector policy を見直します。

## After running

```bash
uv run baibai-loop-validate
```
