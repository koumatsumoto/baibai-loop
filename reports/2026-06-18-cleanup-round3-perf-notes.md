---
title: "Cleanup round 3 報告 + 次回 perf 施策候補"
summary: "Round 3 で削減した内容、削除を見送った機能 (forward 計測経路があるため)、次回パフォーマンス向上施策の候補を記録。"
doc_type: report
status: active
date: 2026-06-18
related_docs:
  - "../docs/operations/backtest-runbook.md"
  - "../docs/anti-patterns.md"
---

# Cleanup round 3 報告

## 1. 削除した実装

| 対象 | 削減 LOC (src/tests/docs) | 削除理由 |
| --- | --- | --- |
| `HorizonReturn.benchmark_return` field | src -3 / tests -1 | aggregate / replay / ablation 全てで read 経路なし。`relative = return_ratio - benchmark_return` の中間値で、`relative` だけが下流に流れる |
| `selection.diagnostics.concentration` block と `previous_overlap.overlap_tickers` | src -11 / tests -2 / docs +3-5 | `recommended[].sector_33` / `selection_lane` を直接 count すれば復元可能。`previous_overlap.overlap_count`/`overlap_ratio` は warning trigger のため保持 |
| `ProfileWeekResult.previous_overlap` / `.concentration` field + `replay_to_payload` 転記 | src -6 | sweep payload の冗長転記。recommended_tickers が serialize されているので不要 |
| `tests/helpers/screening_sqlite.add_source_coverage` 統合 | tests -23 (net) | 4 ファイルで verbatim 重複していた SQL INSERT を helper に集約 |
| `effective-date` 用語整理 | src -1 / docs -1 | code field として存在しない用語の docstring 残骸を `asof gate` / `asof-tagged YAML` に統一 |

**合計**: src -21 / tests -26 / docs +0 程度 / total -47 LOC

## 2. multi-period backtest no-regression 検証

6 週分 (2026-05-01 / 05-08 / 05-15 / 05-29 / 06-11 / 06-12) で baseline replay を取り、各 wave 後に再 replay して diff を取った。

| Wave | 内容 | recommended_tickers 一致 | mean_relative diff |
| --- | --- | --- | --- |
| 1 | HorizonReturn.benchmark_return 削除 | 6/6 週完全一致 | +0.00pt |
| 2 | concentration diagnostics + replay 転記整理 | 6/6 週完全一致 | +0.00pt |
| 3a | add_source_coverage helper 統合 (test infra) | 6/6 週完全一致 (src 不変) | +0.00pt |

cached output: `.cache/backtest/2026-06-18-round3-{baseline,wave1,wave2,final}-replay.yaml`

## 3. 削除を見送った候補 (forward 計測経路の存在)

CLAUDE.md「forward 計測経路を 1 行で説明できるか」self-check に従い、以下は削除対象から外した。

### 3.1 `paper_proxy` 抽象 (~263 LOC)

- `policy_config.paper_proxy_capital_yen` と `validate/research/sizing.py` (201 LOC) が ADV 5% 過参加検証と `paper_to_real_order_notional_pct=21%` 換算の二重 forward 計測経路を担う
- `records/05-research/2026/{05,06}/*.md` 13 件 + `records/06-trades/2026/{05,06}/*.md` 8 件 = 21 件が `paper_proxy_position_size_yen` を要求
- CLAUDE.md 例外「実取引が絡む order/fill audit」に該当
- 削除すると validator 全再設計が必要で `~200 LOC` では収まらない

### 3.2 `src/baibai_loop/screening/freshness.py` (237 LOC + tests 230 LOC)

- `detect_edinet_freshness_warnings()` → `ScreenedCandidate.freshness_warnings` → `render.py:152` で `sizing_eligible=False` → `selection/ranking.py:80-90` の `_sizing_eligible_evidence_hits` で **選定排除**
- `ledger/lane_cohorts.py:215-218` の lane cohort も同じ gate を踏襲
- 「stale EDINET → サイズしない」trade-safety gate であり、CLAUDE.md AP-08「計測経路のない機能は追加しない」の逆原則「計測経路のある trade gate は削らない」に該当
- 削減候補からは恒久的に除外

### 3.3 `effective_date` field 撤廃 (想定 ~30 LOC) → doc 5 行で完了

調査の結果、フィールドとして schema にも src にも records にも存在しないことが判明。`asof` / `asof_date` が実装されている近接概念で、retro-fit 対象 record は 0 件。doc 用語整理のみ。

## 4. 次回 (PR N+1) パフォーマンス向上施策の候補

ユーザ予告「次回はパフォーマンス向上のための施策を対応する予定」に向けた候補リスト。優先度順。

### 4.1 (高) `aggregate_forward_returns` の二重 list comprehension 統合

`src/baibai_loop/ledger/forward_return.py:135-147` で同じ horizon に対して 2 回 nested list を作っている (returns 用と relatives 用)。1 pass で両者を集める形に書き直せば O(N) コスト半減。

```python
# 現状: returns と relatives で 2 回 walk
# 改善: 1 pass で 2 つのリストを作る
```

レイテンシ影響: replay の per-week aggregation 部分。約 100ms 程度の改善見込み (recommended 10件 × 6 週 × 2 horizon)。

### 4.2 (高) `compute_market_regime` の sqlite 再接続 → connection 再利用

`screening_replay.run_replay` で各週ごとに `compute_market_regime(sqlite_path, spec.asof)` を呼び、内部で sqlite を開き閉じしている (`src/baibai_loop/screening/regime.py:88-`)。6 週分 = 12 回の open/close。connection を 1 度開いて使い回せば I/O 削減。

レイテンシ影響: replay 全体で 200-300ms 程度。

### 4.3 (中) `load_bars_for_tickers` の sqlite indexing 確認

`forward_return.py:173-205` で `WHERE ticker IN (...)` で 100+ ticker の bars を fetch。`jquants_daily_bars(ticker, traded_at)` index が効いているか確認。EXPLAIN QUERY PLAN で確認推奨。

### 4.4 (中) `_populate_complete_coverage` (test_sqlite_coverage.py) と `_populate_screening_fixture` (test_sqlite_integration.py) の統合

両者は同じ「全 9 テーブルに最小限の整合 row を流し込む」builder の別実装で、コアロジック (master / bars / fin / earnings_cal / market_calendar / edinet_metrics / jpx_regulation) の ~80% 重複。`tests/helpers/screening_sqlite.populate_minimal_screening_db(conn, asof, *, tickers, include_edinet, include_jpx)` のパラメータ違いに統合可。

LOC 削減見込み: ~250 LOC。ただし pytest fixture 化を伴うと migration コストが高いので別 PR 推奨。

### 4.5 (中) `_insert_bars` / `_insert_daily_bars` 4-5 ファイル統合

`tests/helpers/screening_sqlite.insert_daily_bars(conn, ticker, *, closes=None, ranges=None, end_date, columns)` で mode A (closes pattern) と mode B (range fill pattern) を吸収。LOC 削減見込み: ~120 LOC。

### 4.6 (低) `tempfile.TemporaryDirectory()` scaffold → pytest tmp_path fixture

unittest 系の `TemporaryDirectory()` + `sqlite_path = Path(tmp) / "market.sqlite"` + `open_connection` + try/finally の 4-5 行 scaffold が ~120 箇所で繰り返されている。pytest tmp_path fixture に置き換えると 1 test あたり 3-5 行短縮。

LOC 削減見込み: ~400 LOC。unittest → pytest 移行が伴うため大規模 PR。

### 4.7 (低) sweep loop の並列化

`screening_replay.run_replay` の `for spec in weeks: for profile in profiles: ...` を `concurrent.futures.ThreadPoolExecutor` で並列化。週数 × profile 数の積で線形 wall-clock 短縮。

ただし read-only の sqlite 共有なので thread-safety 検証必須。

## 5. round1 + round2 + round3 累積効果

| 軸 | main baseline | round3 後 | 削減 | 削減率 |
| --- | ---: | ---: | ---: | ---: |
| src/ | 21,034 | 19,283 | **-1,751** | **-8.3%** |
| tests/ | 15,098 | 13,623 | **-1,475** | **-9.8%** |
| docs/ | 5,926 | 4,361 | -1,565 | -26.4% |
| schemas/ | 745 | 692 | -53 | -7.1% |

3 PR で **-4,844 LOC**、forward 計測 no-regression を維持。
