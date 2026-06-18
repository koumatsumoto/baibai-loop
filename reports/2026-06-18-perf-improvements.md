---
title: "Perf 施策 PR 報告 — CSafeLoader + payload cache + 計測ベース採否判断"
summary: "screening-replay の wall time を 17.26s → 2.15s (-87.5%, 8.03x) に短縮。前 PR (#249) の 7 候補に加えて R5 review で発見された YAML payload cache を実装。計測結果と保守性 trade-off を実測ベースで判断。"
doc_type: report
status: active
date: 2026-06-18
related_docs:
  - "../docs/operations/backtest-runbook.md"
  - "./2026-06-18-cleanup-round3-perf-notes.md"
---

# Perf 施策 PR (round 4) 報告

前 PR ([#249](https://github.com/koumatsumoto/baibai-loop/pull/249)) で挙げた 7 perf 候補と、5 名 review で追加発見された候補を、cProfile と wall time 計測で 1 件ずつ採否判断した。

## 1. 結果サマリ

| Wave | 内容 | wall mean (5 runs) | stdev | 削減 vs prev | 採否 |
| ---: | --- | ---: | ---: | ---: | :-: |
| 0 | baseline (PR #249 後 main) | 17.264s | 0.229s | — | — |
| 1 | + CSafeLoader 全 reader 化 (施策8 NEW) | 3.212s | 0.035s | **-81.4%** | **採用** |
| 2 | + YAML payload cache (施策9 R5 発見) | 2.151s | 0.017s | **-33.0%** | **採用** |
| — | (試行) + ProcessPool(4) (施策7-rev R5) | 2.054s | 0.028s | -4.5% | 見送り |

**累積 17.264s → 2.151s = -15.11s, -87.5% (8.03x faster)**。reviewer 独立検証では Wave 1 単独で `-80.3% (5.07x)` と若干差があるが system load noise 内。出力 YAML は md5 byte-identical (`0c328d05ec869dea9016f64ed77a5e85`)、multi-period backtest 6/6 週 recommended_tickers 完全一致。

## 2. 計測手順

```bash
# cProfile で hot path 特定
uv run python -c "import cProfile, sys; sys.argv=['baibai-loop-ledger', 'screening-replay', '--candidates-root', '.cache/replay/candidates', '--regime-lens', 'on', '--top', '10', '--out', '/tmp/r.yaml']; from baibai_loop.ledger.cli import main; cProfile.run('main()')"

# wall time 5 runs 平均
for i in 1..5; uv run baibai-loop-ledger screening-replay --candidates-root .cache/replay/candidates --regime-lens on --top 10 --out /tmp/r.yaml; end

# no-regression 検証
diff <(yaml.safe_load .cache/backtest/perf-wave1-csafe-replay.yaml) <(yaml.safe_load .cache/backtest/2026-06-18-round3-baseline-replay.yaml)
```

「意味あり」基準: stdev の **3σ (1.7%)** を超える wall time 改善、または forward 計測影響なしの test infra cleanup。

## 3. 7 候補の採否

### 採用

#### 施策8 (NEW 発見) — YAML CSafeLoader 全 reader 化

cProfile で発見: baseline では YAML パースが wall time の **93%** (66.7s / 71s cumulative)。`load_week_candidates` 42.5s, `load_previous_candidates` 28.1s が 2 大 hot path。

実装:
- `src/baibai_loop/yaml_io.py` 新設: `safe_load(stream) = yaml.load(stream, Loader=yaml.CSafeLoader)`
- src 14 ファイル + tests 6 ファイルの `yaml.safe_load(...)` を `safe_load(...)` に置換
- 未使用 `import yaml` は `ruff --fix` で削除

効果: **-81.4% wall time** (replay), test 22.52s → 22.25s (-1.2%)。

**注意 (P2 follow-up)**: `src/baibai_loop/validate/research/shared.py:22` には既存の private `_YAML_LOADER = getattr(yaml, "CSafeLoader", ...)` がある (lru_cache + path-based キャッシュのため特殊化されている)。yaml_io.safe_load の stream-based セマンティクスでは代替できないので残置。

#### 施策5 — `_insert_bars` (closes-based pattern) 3 モジュール統合

`tests/helpers/screening_sqlite.insert_daily_bars_from_closes(sqlite_path, ticker, closes, *, end_date, turnover_value=None)` に統合。市場 snapshot / regime / ticker_profile の 3 ファイルで同一 SQL を共有。

効果: tests/ net -14 LOC、SQL 重複解消。test wall time に影響なし (helper 化は overhead ゼロ)。

ablation / coverage / jquants 系の特殊形 (weekly growth / range fill / range tuple) は無関係パターンのため touch せず。

### 採用見送り (計測根拠付き)

#### 施策1 — `aggregate_forward_returns` 1 pass 化

CSafeLoader 後の cProfile で cumtime <0.1s (top 25 にも入らず)。改善余地は noise threshold (1.7%) を下回る。コード保守性目的なら別 PR で実施可。

#### 施策2 — `compute_market_regime` の sqlite connection 再利用

当初 estimate「12 回 × ~10ms = ~120ms」「3.7% 改善」だったが、R5 review の独立 cProfile 計測で `compute_market_regime` の cumtime は **0.001s (0.013%)** と判明。estimate は ~200x 過大評価していた。defer 判断自体は正解 (改善余地 1.7% 閾値の遥か下) だが、estimate なしで「multiplied X 回」を理由にすると future-self が再追跡する罠になるので、profile せずに見積もる anti-pattern を AP-10 でも codify。

#### 施策3 — `load_bars_for_tickers` indexing 確認

EXPLAIN QUERY PLAN 結果:
```
SEARCH jquants_daily_bars USING INDEX sqlite_autoindex_jquants_daily_bars_1 (ticker=?)
```
PRIMARY KEY `(ticker, traded_at)` の自動 index が既に使われている。追加 index 不要。

#### 施策4 — `_populate_*` builder 統合

`_populate_complete_coverage` (100 tickers, minimal fin) と `_populate_screening_fixture` (1 ticker, full fin+edinet+jpx) はパラメータ化に大規模リファクタが必要。LOC win 250 はあるが wall time 改善は微小 (~0.1s)。perf 文脈では「意味あり」基準未達。test infra cleanup として将来別 PR で対応。

#### 施策6 — `tempfile.TemporaryDirectory` → `pytest tmp_path` 移行

unittest → pytest 移行は影響範囲広大 (~120 箇所)。LOC win 400 + 構造改善が主、wall time は数百 ms 程度。perf 文脈では「意味あり」基準未達。test infra cleanup として将来別 PR。

#### 施策7 — sweep loop 並列化

`ThreadPoolExecutor(max_workers=8)` で `_build_week_sweep` 並列化を実装→計測したが **3.212s → 3.372s (+5% 悪化)**。CSafeLoader は libyaml 内部で GIL release しているが、Python 側 constructor が GIL bound のため thread parallel 効かず、ThreadPool オーバーヘッドだけ載って negative。revert 済み。

#### 施策7-rev — sweep loop 並列化 (ProcessPoolExecutor)

R5 review で「ProcessPool は GIL を回避できる」と指摘されたため再評価。`ProcessPoolExecutor(max_workers=4)` で `_build_week_sweep_for_pool` (pickle 可能な tuple-arg wrapper) を実装。

実測 (5 runs): payload_cache 経由 **2.151s** → +ProcessPool **2.054s** = **-97ms (-4.5%)**。

判断: 3σ noise threshold (1.7%) は超えるが、以下の保守性コストと trade-off で **見送り**:
- multiprocessing wrapper (_build_week_sweep_for_pool) と tuple-arg pickle 制約
- payload_cache が worker 間で共有不可 → 各 worker で再 parse 発生
- pickle overhead で 4MB dict を main プロセスに戻す
- Mac/Windows fork セマンティクス差分が test 環境で不安定要因

「実測 4.5% 改善は保守性コストに見合わない」を数値で記録した。GIL 理論ではなく実測で defer。

#### 施策9 (R5 NEW) — YAML payload cache **採用**

R5 review が独立 cProfile で発見した最大 win。`run_replay` で `load_week_candidates(N)` と `load_previous_candidates(N+1)` が同じ ~4MB YAML を二重 load していた事実。post-CSafeLoader の hot path は `yaml/constructor.py:get_single_data` (Python-side construction ~1.6s tottime)、これを cache でスキップ。

実装: `dict[Path, Mapping[str, object]]` を `run_replay` スコープで共有、`load_week_candidates(path, *, payload_cache=None)` と `load_previous_candidates(..., *, payload_cache=None)` の両方に optional 引数。外部 caller (CLI) は変更なし。

効果: **2.151s vs 3.212s = -1.06s, -33%**。累積 baseline からは **-87.5% (8.03x faster)**。R5 予測 (3.0→2.0s) と完全一致。

## 4. 次回 PR への follow-up

reviewer 5 名から indicated 改善候補:

| | 内容 | 優先度 |
| --- | --- | :-: |
| F1 | `validate/research/shared.py:22` `_YAML_LOADER` を yaml_io.py に統合 (lru_cache 用に新 API 追加検討) | P2 |
| F2 | `yaml.dump` / `yaml.safe_dump` 6 箇所 (cli/query.py L77, L109, L163 / ledger/cli.py L221, L257, L289) を `yaml.CSafeDumper` 経由に切り替え | P2 |
| F3 | `yaml_io.safe_load` の CSafeLoader 不在 fallback path に `warnings.warn(...)` 追加 (silent perf regression 防止) | P2 |
| F4 | `tests/test_yaml_io.py` 追加 (fallback path / float / date / unicode の挙動確認) | P2 |
| F5 | `docs/anti-patterns.md` AP-10: 「perf-critical YAML callers は必ず `baibai_loop.yaml_io.safe_load` 経由を使う」を codify | P2 |
| F6 | 施策4 `_populate_*` builder consolidation (test infra cleanup PR) | P3 |
| F7 | 施策6 `tempfile` → `tmp_path` migration (test infra cleanup PR) | P3 |

## 5. レビュー結果 (5 名 adversarial)

- **R1 (perf claim)**: VERIFIED、5.07x (claim 5.4x は noise 内)、md5 byte-identical no-regression、CSafeLoader present (yaml 6.0.3, `__with_libyaml__=True`)。P0/P1 なし。
- **R2 (test helper safety)**: 3 モジュールとも behavioral 完全一致、wrapper signature 互換、edge case (空 closes / 0.0 turnover) OK。collision なし。P0/P1 なし。
- **R3 (yaml migration completeness)**: 全 41 reader 移行確認、unsafe loader (yaml.load without Loader) ゼロ、resolver semantics divergence なし。P2 = 3 test の import 順 (修正済み)、P3 = shared.py の private \_YAML_LOADER (lru_cache 経由のため統合困難、residual)。
- **R4 (docs alignment)**: P0 = backtest-runbook §6 行追加 (済) + 本 report 作成 (済)、P1 = anti-patterns AP-10 + yaml_io docstring 強化 (済) + shared.py 整理 (defer)。
- **R5 (deferred-candidates audit)**: §4.2 estimate ~200x 過大評価を発見 (200-300ms → 実測 <1ms)、**最大 win YAML payload cache を発見** (33% wall time, 採用済 +1.06s 改善)、ProcessPool は実測 4.5% 改善のみで GIL 理論ではなく数値で defer 確定。

## 6. 反映 commit (4 件)

| commit | 内容 |
| --- | --- |
| 99b4b6c | perf(yaml): 全 reader を CSafeLoader 経由 (-81.4% wall) |
| 67716c5 | test(helpers): \_insert_bars 3 module consolidate (-14 LOC) |
| f966483 | docs(perf): R3 import 順 + AP-10 + yaml_io docstring |
| eafae57 | perf(replay): payload cache (-33% wall, 累積 8.03x) |
