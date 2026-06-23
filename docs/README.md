---
title: "Baibai-Loop docs portal"
summary: "Reader-oriented entry point for architecture, component contracts, operations, reference, governance, templates, and screening subsystem docs."
doc_type: portal
status: active
last_reviewed: 2026-05-04
---

# Baibai-Loop docs

`docs/` は Baibai-Loop の仕様と運用ルールの正本入口です。root `README.md` は初見向けの概要に留め、判断根拠、成分契約、運用手順、検証基盤はこの portal から辿ります。

## 読む順番

| 読者 | 最初に読む docs | 目的 |
| --- | --- | --- |
| 初めて repo を触る人 | [`concepts.md`](./concepts.md) -> [`architecture/system-overview.md`](./architecture/system-overview.md) -> [`philosophy.md`](./philosophy.md) -> [`design-principles.md`](./design-principles.md) | 何を作っているか、なぜこの形か、守る原則を把握する |
| records を作成する人 | [`operations/README.md`](./operations/README.md) -> [`components/README.md`](./components/README.md) | 手順入口と成果物ごとの contract を確認する |
| screening / automation を触る人 | [`architecture/automation-map.md`](./architecture/automation-map.md) -> [`screening/README.md`](./screening/README.md) -> [`reference/testing-and-validation.md`](./reference/testing-and-validation.md) | CLI、schema、validation、screening subsystem の境界を確認する |
| Python 基盤を変更する人 | [`reference/python-foundation.md`](./reference/python-foundation.md) | runtime、dependency、quality gate、CI と local parity を確認する |

## 区分

| 区分 | 責務 | 主な docs |
| --- | --- | --- |
| `architecture/` | 現在の構造、情報フロー、repository map、automation map、ADR | [`architecture/README.md`](./architecture/README.md) |
| `concepts.md` | 投資判断プロセスの正準ドメインモデルと用語境界 | [`concepts.md`](./concepts.md) |
| `components/` | `records/` に残る成果物の contract | [`components/README.md`](./components/README.md) |
| `operations/` | いつ、どう作るかの runbook 入口 | [`operations/README.md`](./operations/README.md) |
| `reference/` | data sources、configuration、layout、validation、Python foundation | [`reference/README.md`](./reference/README.md) |
| `governance/` | anti-pattern 運用の governance 入口 | [`governance/README.md`](./governance/README.md) |
| `templates/` | artifact 作成時にコピーする template | [`templates/README.md`](./templates/README.md) |
| `screening/` | candidates 生成を支える subsystem 詳細 | [`screening/README.md`](./screening/README.md) |

## 変更時に更新する docs

| 変更内容 | 併せて見る docs |
| --- | --- |
| `records/01-macro-context/` の作成・変更 | [`components/macro-context.md`](./components/macro-context.md), [`reference/data-sources.md`](./reference/data-sources.md) |
| `records/04-candidates/` または screening CLI の変更 | [`components/candidates.md`](./components/candidates.md), [`screening/README.md`](./screening/README.md), [`architecture/automation-map.md`](./architecture/automation-map.md) |
| `records/05-thesis/` の作成・変更 | [`operations/research-runbook.md`](./operations/research-runbook.md), [`components/research.md`](./components/research.md), [`components/playbooks.md`](./components/playbooks.md) |
| `records/06-position/` の作成・変更 | [`components/trades.md`](./components/trades.md), [`operations/trade-runbook.md`](./operations/trade-runbook.md) |
| schema / validator / tests / CI の変更 | [`reference/testing-and-validation.md`](./reference/testing-and-validation.md), [`reference/python-foundation.md`](./reference/python-foundation.md), [`architecture/automation-map.md`](./architecture/automation-map.md) |

## 正本の境界

- 思想は [`philosophy.md`](./philosophy.md) に残す。多数の既存相対リンクがあるため移動しない。
- 設計原則は [`design-principles.md`](./design-principles.md) に残す。節番号参照があるため移動しない。
- 失敗パターンの正本は [`anti-patterns.md`](./anti-patterns.md) に残す。governance 側は運用入口を置く。
- component contract は [`components/`](./components/) に残す。既存 `components/*.md` の path と節構造は凍結する。
- Root 直下に分散した内容は置かず、参照先は `operations/`, `architecture/`, `reference/` の正本へ寄せる。
