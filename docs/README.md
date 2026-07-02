---
title: "Baibai-Loop docs portal"
summary: "思想・構造・運用方針・工程手順・参照情報・失敗パターンへの入口。root README は初見向け概要、判断根拠と手順はこの portal から辿る。"
doc_type: portal
status: active
last_reviewed: 2026-07-02
---

# Baibai-Loop docs

`docs/` は Baibai-Loop の仕様と運用ルールの正本入口。root `README.md` は初見向け概要に留め、思想・構造・工程手順・参照情報はこの portal から辿る。

## 読む順番

| 読者 | 最初に読む docs | 目的 |
| --- | --- | --- |
| 初めて repo を触る人 | [`doctrine.md`](./doctrine.md) → [`architecture.md`](./architecture.md) | 何を信じ何を狙うか、どの構造で動くかを把握する |
| 運用する人 | [`portfolio-management.md`](./portfolio-management.md) → [`workflow/README.md`](./workflow/README.md) | 資本方針と、単一ループの各工程手順を確認する |
| screening / automation を触る人 | [`architecture.md#automation`](./architecture.md#automation) → [`workflow/screening.md`](./workflow/screening.md) → [`reference/screening-runtime.md`](./reference/screening-runtime.md) | CLI・SQLite・screening 工程・実装仕様を確認する |
| Python 基盤を変更する人 | [`reference/python-foundation.md`](./reference/python-foundation.md) | runtime・dependency・quality gate・CI parity を確認する |

## 区分

| 区分 | 責務 |
| --- | --- |
| [`doctrine.md`](./doctrine.md) | 投資思想・大戦略・原則・語彙（正準ドメインモデル） |
| [`architecture.md`](./architecture.md) | 構造・3 層・7 package・repository map・CLI/SQLite 安定契約 |
| [`portfolio-management.md`](./portfolio-management.md) | 資本・ポジション管理・cap・積立・余力・kill switch |
| [`anti-patterns.md`](./anti-patterns.md) | 失敗パターンと commit 前チェックリスト |
| [`workflow/`](./workflow/) | 単一ループ各工程の手順（macro / screening / research / position / playbooks） |
| [`reference/`](./reference/) | valuation-metrics・screening-runtime・data-sources・python-foundation・configuration・testing-and-validation・jquants-rate-limits |
| [`operations/`](./operations/) | 工程横断の手順（task-runbook / incident-runbook） |

## 変更時に併せて更新する docs

| 変更内容 | 併せて見る docs |
| --- | --- |
| `records/01-macro-context/` | [`workflow/macro.md`](./workflow/macro.md), [`reference/data-sources.md`](./reference/data-sources.md) |
| `records/02-candidates/` または screening CLI | [`workflow/screening.md`](./workflow/screening.md), [`reference/screening-runtime.md`](./reference/screening-runtime.md), [`architecture.md#automation`](./architecture.md#automation) |
| `records/03-thesis/` | [`workflow/research.md`](./workflow/research.md), [`workflow/playbooks.md`](./workflow/playbooks.md) |
| `records/04-position/` | [`workflow/position.md`](./workflow/position.md) |
| schema / validator / tests / CI | [`reference/testing-and-validation.md`](./reference/testing-and-validation.md), [`reference/python-foundation.md`](./reference/python-foundation.md), [`architecture.md#automation`](./architecture.md#automation) |

## 正本の境界

- 思想・原則・語彙は [`doctrine.md`](./doctrine.md)。安定アンカー `#vocabulary` / `#fact-analysis-separation` を切らない。
- 構造・repository map・automation は [`architecture.md`](./architecture.md)。安定アンカー `#repository-map` / `#automation`。
- 成果物の機械契約は `records/_schemas/*.json`（contract-of-record）。工程 doc は field を書き写さず、意味・計算式・設計判断の理由だけを持つ。
- 失敗パターンの正本は [`anti-patterns.md`](./anti-patterns.md)。AP 番号は維持する。
