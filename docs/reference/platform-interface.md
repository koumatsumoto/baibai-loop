---
title: "Platform interface"
summary: "AI / スクリプトが基盤を利用するための 2 つの安定契約(CLI YAML 出力と SQLite schema)と利用モデル。"
doc_type: reference
status: active
last_reviewed: 2026-06-23
related_docs:
  - "../screening/automation.md"
  - "../screening/extending.md"
  - "../philosophy.md"
---

# Platform interface

Baibai-Loop をデータ解析基盤として AI / スクリプトが利用するための契約。安定化対象は次の **2 面だけ** であり、これ以外(Python 内部 API、`.cache/` の中間物)は予告なく変わる。

## 契約 1: CLI の YAML 出力

| コマンド | 出力 | 用途 |
| --- | --- | --- |
| `baibai-loop-screening run` | `records/04-candidates/<Y>/<M>/<date>.yaml` | 週次 screen output(事実) |
| `baibai-loop-screening select` | stdout YAML(`recommendations` + `selection.diagnostics`) | research 候補の triage |
| `baibai-loop-screening ticker-profile` | stdout YAML(1 銘柄の事実 packet、保有ポジション集中度を含む) | 個別銘柄リサーチと entry 前チェックの起点(全上場銘柄対応) |
| `baibai-loop-screening market-snapshot` | stdout YAML(regime 履歴 + sector 集計) | 市況リサーチの起点、macro context の機械入力 |
| `baibai-loop-screening screening-replay` | replay payload YAML | profile / lens の forward 計測 |
| `baibai-loop-screening playbook-cohorts` | playbook cohort payload YAML | playbook 母集団の forward 計測 |
| `baibai-loop-screening selection-ablation` | ablation payload YAML | ranking 機能別の効果計測 |
| `baibai-loop-macro` | 統計 series cache | macro context の入力 |

安定性の意味:

- field の**追加**は随時行う。既存 field の名前と意味は黙って変えない
- 既存 field の意味を変える場合は、関連 doc(`docs/screening/`)とこの doc を同一 PR で更新する
- 出力はすべて機械可読(YAML)で、人間向け整形は stdout サマリに分離する

## 契約 2: SQLite schema(`data/screening/market.sqlite`)

- テーブル定義と意味論の正本は [`../screening/automation.md`](../screening/automation.md) §11.1
- schema は `PRAGMA user_version` で版管理し、破壊的変更は version bump + rebuild(migration はしない)
- 対象範囲は**全上場銘柄**。時価総額・流動性での絞り込みはテーブルには存在せず、分析側が必要に応じて適用する
- AI は読み取り専用で SQL を直接発行してよい。書き込みは CLI(bootstrap / extract / run)経由に限る

主要テーブル(詳細は automation.md):

| table | 内容 |
| --- | --- |
| `jquants_daily_bars` | 日足 OHLCV・adjusted close(全上場銘柄、約 1,200 日) |
| `jquants_fin_summaries` | 財務サマリー・会社予想 |
| `jquants_master_snapshots` | 銘柄マスター(33 業種・市場区分) |
| `edinet_metrics` | EDINET CSV-derived の厳密指標(net cash / FCF / capex、TTM quality 付き) |
| `jpx_regulation_flags` | 規制 flag(特別注意・整理・取引停止等) |

## AI の利用モデル

- **L1/L2 は自由に読む**: SQL 直接 + CLI 出力。すべての主張は queryable な事実に遡れる形で書く(AP-01: 一次情報主義)
- **L3 は下書きまで**: research memo / review の下書きは AI が作ってよいが、最終採用判定・失敗分類確定・macro context 前提確認は人間が行う([`../components/thesis.md`](../components/thesis.md) の「AI の役割境界」)
- **スコアの扱い**: 基盤が出すスコアは軸別の座標(sector 相対・自己レンジ相対など)であり、売買判定ではない。AI はスコアを根拠の 1 つとして引用し、単独で結論にしない

## 非目標

MCP server / API server 化、リアルタイム配信、第三者向けサービングは行わない。single-operator・local-first を維持し、AI はこのリポジトリの作業環境内で CLI と SQLite を直接使う。
