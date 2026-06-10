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

`bootstrap-cache` / `extract-edinet-metrics` / `run` は、長時間止まって見えないようにデータソース別の進捗を stdout に出します。`run` の exit code `2` は YAML 生成後の partial warning であり、stdout の `screening run partial warning reasons` を見て EDINET TTM 近似や YoY 欠損の量を確認します。

`--output-path .cache/...` のような scratch 出力では、正式な `records/04-candidates/` を汚しません。universe snapshot は保存せず、必要な母集団確認は実行時の `universe_size` と cache coverage で行います。


## Select research candidates

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD
```

`select` は最新 candidates と macro context を組み合わせ、research 候補の triage を支援します。最終採用判断は [`../components/research.md`](../components/research.md) の boundary に従い、人間が確定します。

`select` は macro context で候補を自動除外せず、lane-specific metric、短期 dislocation、long-hold survivability、過去 research decision を組み合わせて triage します。Hit 数と時価総額だけで機械的に上位化しません。

出力は `recommendations` と `selection.diagnostics` を正本にします。default は daily triage 用 summary で、候補の full `lenses` や debug detail が必要な場合だけ `--detail full` を付けます。

- `recommendations`: research 着手候補。`selection_lane` と `selection_metrics` を見て primary thesis を決める。
- `lenses.fast_dislocation`: 明確な価格下落と fundamental guard を同時に満たすかを示す annotation。rank と reason tag に反映する。
- `lenses.long_hold_survivability`: 短期 thesis が外れた場合の保有耐性を `high|medium|low|unknown` で示す annotation。
- `selection.diagnostics`: suppressed count、previous overlap、sector / lane concentration、fast / long-hold の件数、warnings を確認する。

件数は `--top` と `output.research_selection_target_max` で調整します。複数 hit 銘柄では、config の lane order に従って `selection_lane` を選びます。

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

`select-sweep` では profile ごとの recommended tickers、recommended detail、fast-dislocation count、long-hold count、suppressed count、profile 間の added / removed / changed、previous overlap、sector / lane concentration を比較します。運用設定を変える前に、最低 6-8 週の実データで `strict` / `balanced` / `loose` と候補 profile を比較し、直近 1-2 週を hold-out として残します。forward-only 原則に従い、ここでは過去 fit ではなく「profile を採用する前の再現性確認」として扱います。

Profile を採用する前に、少なくとも以下を表にします。

- `recommended_tickers`: 望ましい thesis の候補が出ているか
- `fast_dislocation_count`: 閾値が広すぎて fast 候補を量産していないか
- `recommended[].selection_lane`: 特定 lane が recommended を埋め尽くしていないか
- `recommended[].fast_guard_count` / `fast_guard_family_count` / `fast_confidence` / `fast_data_status` / `long_hold_rating`: 売られ過ぎと財務健全性の両方を満たしているか
- `recommended_diff_vs_first_profile`: ticker 入替だけでなく rank / confidence / lane の変化が妥当か
- `previous_overlap` と `concentration`: 前回候補・同一 sector / lane への偏りが再発していないか
- `warnings`: `recommendations_high_previous_overlap`、`invalid_numeric_metric_values`、`short_return_price_history_missing` が残っていないか

サイロ化を避けるため、既定 profile は `selection.diversity.max_previous_candidates_in_recommended` で前回 candidates 由来の銘柄数に上限を置きます。上限に達した場合は新規候補を優先し、最低件数を満たすための緩和は行いません。

Lane ごとの hit 数は `evidence_hits_summary` で確認します。特定 lane が universe の大きな割合を占める場合は、候補数が増えただけで evidence の識別力が弱い可能性があるため、`records/_config/screening-rules/2026-05-01T000000+0900.yaml` の閾値・sector policy を見直します。

## Multi-week replay と forward return

`select-sweep` は 1 週分の profile 比較です。複数週を跨いだ recommended queue の forward return 評価は `baibai-loop-ledger screening-replay` で行います。profile を採用・変更する前に、6-8 週の実データで recommended forward return を benchmark proxy 比で比較し、直近 1-2 週を hold-out として残します。

```bash
# 1. 各週の candidates を scratch root に生成（週ごとに J-Quants rate budget が要る）
for W in 2026-04-10 2026-04-17 2026-04-24 2026-05-15 2026-05-22 2026-05-29; do
  uv run baibai-loop-screening bootstrap-cache --asof "$W"
  uv run baibai-loop-screening extract-edinet-metrics --asof "$W" --lookback-days 540
  uv run baibai-loop-screening run --asof "$W" --allow-stale-jpx \
    --output-path .cache/replay/candidates/${W:0:4}/${W:5:2}/$W.yaml --force
done

# 2. profile を replay し recommended forward return を集計
uv run baibai-loop-ledger screening-replay \
  --candidates-root .cache/replay/candidates \
  --profiles strict,balanced,loose --holdout-weeks 2 \
  --out .cache/replay/replay-payload.yaml
```

- forward return は asof + 1w / 4w / 8w を J-Quants 日足から算出し、日経225 ETF proxy `1321` 比の relative を出す。eval cap（cache 最新足）を超える horizon は unresolved として集計から除外する。価格基準は [`../reference/data-sources.md`](../reference/data-sources.md) §Benchmark proxy。
- replay は macro-agnostic で回す。non-stale macro context が揃わない過去週でも profile 選定機構を比較できる。
- market regime lens は default で各週に適用される（`--regime-lens off` で従来挙動）。on/off 比較の手順と 2026-05 の検証結果は [`../screening/regime-lens-replay-2026-05.md`](../screening/regime-lens-replay-2026-05.md)。
- 歴史週の `bootstrap-cache` は asof ごとに長期履歴を取り直すため、J-Quants throttling 下では 1 週で数時間かかりうる。chunk は resumable なので kill せず完走させる。挙動の詳細は [`../reference/jquants-rate-limits.md`](../reference/jquants-rate-limits.md)。
- 評価結果と `balanced` 継続可否の判断は [`../screening/replay-2026-05.md`](../screening/replay-2026-05.md) に記録する。

## Lane cohort telemetry

replay が recommended queue（top N）だけを評価するのに対し、`lane-cohorts` は週次 candidates の**全銘柄**を evidence lane 別 cohort として forward return を集計し、playbook 改訂ループの一次資料を作る。

```bash
uv run baibai-loop-ledger lane-cohorts \
  --candidates-root records/04-candidates \
  --horizons 1,4 --out .cache/replay/lane-cohorts-latest.yaml
```

- lane 抽出は sizing-eligible な evidence hit に限る（selection と同じ意味論: `source_status` が ok 以外の文字列なら除外、`sizing_eligible: false` なら除外）。複数 lane hit は各 cohort に計上し、`all_candidates` baseline を併記する
- 直近週は eval cap 未到達で `resolved 0` になる。月次 retro 時点で再実行すれば forward-only で埋まる
- 初回スコアボードと解釈の限界は [`../screening/lane-cohorts-2026-05.md`](../screening/lane-cohorts-2026-05.md)。lane 序列の解釈・playbook 改訂は retro 側で扱う（事実と分析の分離）

## Selection ablation

selection の ranking 構成要素や lane を 1 つずつ無効化した variant 群を replay し、各機能の forward return 寄与(`Δfull`)と推奨 queue の重複率を計測する。機能の削減・維持判断の根拠データを作るときに使う。

```bash
uv run baibai-loop-ledger selection-ablation \
  --candidates-root .cache/replay/candidates \
  --out .cache/replay/selection-ablation-latest.yaml
```

variant は事前列挙した機能スイッチ(閾値 sweep はしない)。計測結果と解釈は [`../screening/selection-ablation-2026-05.md`](../screening/selection-ablation-2026-05.md)。

## After running

```bash
uv run baibai-loop-validate
```
