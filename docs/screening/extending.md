---
title: "Screening 基盤の拡張ガイド"
summary: "新しい lens / lane / telemetry を追加するときの拡張点と、計測ファーストの検証手順。"
doc_type: reference
status: active
last_reviewed: 2026-06-10
related_docs:
  - "./mechanical.md"
  - "../operations/backtest-runbook.md"
  - "../operations/screening-runbook.md"
---

# Screening 基盤の拡張ガイド

screening / selection / telemetry に機能を足すときの拡張点と検証手順。原則は 1 つ: **機能は足す前に計測経路を確保し、足した後に forward 計測で残す価値を証明する**(計測できない機能は追加しない)。

## レイヤと拡張点の対応

| レイヤ | 場所 | 拡張の典型 |
| --- | --- | --- |
| データ層 | `screening/providers/` + `screening/sqlite_cache/` | 新しい market data source の取り込み(SQLite に正規化、schema version 更新) |
| 指標層 | `screening/metrics.py` + `screening/schema.py` | 新しい財務・価格指標(FinancialSnapshot に field 追加) |
| screen 層(事実) | `screening/rules.py` + `rule_config.py` + `records/_config/screening-rules/` | 新しい lane(playbook-linked screen) |
| selection 層(lens) | `screening/selection/` package | 新しい lens・ranking 成分・profile |
| telemetry 層 | `ledger/`(replay / lane_cohorts / selection_ablation / forward_return) | 新しい計測・スコアボード |

## selection package の module 構成

| module | 責務 | 触るとき |
| --- | --- | --- |
| `records.py` | candidate / prior-research の型と loader | 入力 field を増やすとき |
| `profiles.py` | built-in `balanced` profile と in-process programmatic override (selection-ablation `no_diversity` で使用) | 閾値セットの実験経路 |
| `lenses.py` | per-candidate annotation(fast dislocation / long-hold) | **新 lens はここ** |
| `ranking.py` | sort key 成分と `RankingToggles`(ablation 用スイッチ) | ranking 成分の追加・削除 |
| `macro_fit.py` | macro context fit 診断(soft、gate にしない) | macro 連携の変更 |
| `summaries.py` | reason/risk tag と出力整形 | 出力 field の追加 |
| `payload.py` | ranking + diversity + diagnostics の組み立て | lens を ranking / 出力へ配線 |

## 新しい lens を追加する手順

1. `lenses.py` に lens 関数を書く(`_candidate_lenses` に登録)。閾値は固定値を事前登録し、根拠を docstring に書く(grid search しない)
2. ranking に影響させる場合は `ranking.py` の `RankingToggles` に成分スイッチを追加し、`payload.py` の sort key に toggle 付きで組み込む。**default は必ず従来挙動と一致**させ、テストで担保する
3. `ledger/selection_ablation.py` の `DEFAULT_VARIANTS` に `no_<新成分>` variant を追加する(計測経路の確保)
4. 記録済み candidates で ablation を実行し、`Δfull` と overlap を確認して doc 化する
5. 効果が観測されない成分は入れない(または diagnostics 専用に留める)

regime lens(`screening/regime.py` + payload 配線)が実装の参考例。検証は [`../operations/backtest-runbook.md`](../operations/backtest-runbook.md) の 7 axis に従う。

## 新しい lane(playbook screen)を追加する手順

1. `rule_config.py` に lane の Pydantic 設定クラスを追加し、`records/_config/screening-rules/` に**新しい asof-tagged YAML** を作って閾値を定義する(既存版は履歴として残す)
2. `rules.py` の `evaluate_screening` に評価関数を追加する(null 理由を `null_reasons` に残し、判定の透明性を保つ)
3. lane の優先順位は config の `output.research_selection_lane_order` に追記する(**コードに順序を書かない**。順序の正本は config 1 箇所)
4. 数週分の candidates が貯まったら `lane-cohorts` で母集団の forward return を計測し、`selection-ablation` の `drop_lane:<新 lane>` で推奨 queue への寄与を確認する

## telemetry を追加する手順

- forward return の計算は `ledger/forward_return.py`(`compute_ticker_forward_returns` / `load_bars_for_tickers`)を使う。価格 basis(adjusted close 優先)と eval cap の意味論を変えない
- 週次 candidates の走査は `screening_replay.discover_week_specs` / `WeekSpec` を流用する
- 出力は YAML artifact + stdout サマリの 2 形式(replay / lane-cohorts / ablation と同じ形)。artifact は機械可読を優先する(AI が読む前提)
- 結果 doc は「数値表 + 観察された事実 + 解釈の限界」の 3 部構成で `docs/screening/` に残す

## 削減の手順(機能を消すとき)

1. 参照ゼロか(grep)、発動ゼロか(ablation の overlap 100% / telemetry)を確認する
2. 「発動しなかった」と「論理的に不要」を区別する(prior-research suppression のように運用規律として残すものがある)
3. 互換 shim は残さない。呼び出し側・テスト・docs を同一 PR で更新する
