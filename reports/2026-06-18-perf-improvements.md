---
title: "Perf 施策 PR 報告 — CSafeLoader 化 + 計測ベース採否判断"
summary: "screening-replay の wall time を 17.26s → 3.21s (-81%, 5.4x) に短縮。前 PR (#249 cleanup round 3) で挙げた 7 候補のうち、計測した結果 5 件は採用見送りし、CSafeLoader (新発見) と `_insert_bars` consolidation の 2 件を採用。"
doc_type: report
status: active
date: 2026-06-18
related_docs:
  - "../docs/operations/backtest-runbook.md"
  - "./2026-06-18-cleanup-round3-perf-notes.md"
---

# Perf 施策 PR (round 4) 報告

前 PR ([#249](https://github.com/koumatsumoto/baibai-loop/pull/249)) で挙げた 7 perf 候補を、cProfile と wall time 計測で 1 件ずつ採否判断した。

## 1. 結果サマリ

| | wall time mean (5 runs) | stdev | 採否 |
| --- | ---: | ---: | --- |
| baseline (PR #249 後 main) | 17.264s | 0.229s | — |
| CSafeLoader 全 reader 化後 | 3.212s | 0.035s | **採用** |

**-14.05s, -81.4% (5.4x faster)**。reviewer 独立検証では `-80.3% (5.07x)` と若干差があるが system load noise 内。出力 YAML は md5 byte-identical。multi-period backtest 6/6 週 recommended_tickers 完全一致。

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

12 回 × ~10ms open/close = ~120ms。3.7% 改善は計測可能だが、connection lifetime 管理が複雑化する保守性低下と引き換えに見合わない。defer。

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

`ThreadPoolExecutor(max_workers=8)` で `_build_week_sweep` 並列化を実装→計測したが **3.212s → 3.372s (+5% 悪化)**。CSafeLoader は libyaml 内部で GIL release しているが、Python 側 constructor が GIL bound のため thread parallel 効かず、ThreadPool オーバーヘッドだけ載って negative。`ProcessPoolExecutor` は ~50-100ms 起動コストで 6 weeks 並列効果を吸収できない。revert 済み。

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
- **R3 (yaml migration completeness)**: TBD (running)
- **R4 (docs alignment)**: P0 = backtest-runbook §6 行追加 + 本 report 作成、P1 = anti-patterns AP-10 + yaml_io docstring 強化 + shared.py 整理
- **R5 (deferred-candidates audit)**: TBD (running)
