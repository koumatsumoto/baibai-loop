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
| 初めて repo を触る人 | [`architecture/system-overview.md`](./architecture/system-overview.md) -> [`philosophy.md`](./philosophy.md) -> [`design-principles.md`](./design-principles.md) | 何を作っているか、なぜこの形か、守る原則を把握する |
| records を作成する人 | [`operations/README.md`](./operations/README.md) -> [`components/README.md`](./components/README.md) | 手順入口と成果物ごとの contract を確認する |
| screening / automation を触る人 | [`architecture/automation-map.md`](./architecture/automation-map.md) -> [`screening/README.md`](./screening/README.md) -> [`reference/testing-and-validation.md`](./reference/testing-and-validation.md) | CLI、schema、validation、screening subsystem の境界を確認する |
| docs を変更する人 | [`governance/docs-style-guide.md`](./governance/docs-style-guide.md) -> [`governance/docs-review-process.md`](./governance/docs-review-process.md) | front matter、shim、review 手順を確認する |
| Python 基盤を変更する人 | [`reference/python-foundation.md`](./reference/python-foundation.md) | runtime、dependency、quality gate、CI と local parity を確認する |

## 区分

| 区分 | 責務 | 主な docs |
| --- | --- | --- |
| `architecture/` | 現在の構造、情報フロー、repository map、automation map、ADR | [`architecture/README.md`](./architecture/README.md) |
| `components/` | `records/` に残る成果物の contract | [`components/README.md`](./components/README.md) |
| `operations/` | いつ、どう作るかの runbook 入口 | [`operations/README.md`](./operations/README.md) |
| `reference/` | data sources、configuration、layout、validation、glossary、Python foundation | [`reference/README.md`](./reference/README.md) |
| `governance/` | docs 自体を維持するためのルール | [`governance/README.md`](./governance/README.md) |
| `templates/` | artifact 作成時にコピーする template | [`templates/README.md`](./templates/README.md) |
| `screening/` | candidates 生成を支える subsystem 詳細 | [`screening/README.md`](./screening/README.md) |

## 変更時に更新する docs

| 変更内容 | 併せて見る docs |
| --- | --- |
| `records/01-brief/` の作成・変更 | [`operations/brief-runbook.md`](./operations/brief-runbook.md), [`components/brief.md`](./components/brief.md), [`reference/data-sources.md`](./reference/data-sources.md) |
| `records/02-outlook/` の作成・変更 | [`operations/outlook-runbook.md`](./operations/outlook-runbook.md), [`components/outlook.md`](./components/outlook.md), [`anti-patterns.md`](./anti-patterns.md) |
| `records/03-candidates/` または screening CLI の変更 | [`components/candidates.md`](./components/candidates.md), [`screening/README.md`](./screening/README.md), [`architecture/automation-map.md`](./architecture/automation-map.md) |
| `records/04-research/` の作成・変更 | [`components/research.md`](./components/research.md), [`operations/screening-runbook.md`](./operations/screening-runbook.md), [`components/playbooks.md`](./components/playbooks.md) |
| `records/05-trades/` / `records/06-reviews/` の作成・変更 | [`components/trades.md`](./components/trades.md), [`components/reviews.md`](./components/reviews.md), [`operations/review-runbook.md`](./operations/review-runbook.md) |
| schema / validator / tests / CI の変更 | [`reference/testing-and-validation.md`](./reference/testing-and-validation.md), [`reference/python-foundation.md`](./reference/python-foundation.md), [`architecture/automation-map.md`](./architecture/automation-map.md) |
| docs の移動・分割・shim | [`governance/docs-style-guide.md`](./governance/docs-style-guide.md), [`governance/docs-review-process.md`](./governance/docs-review-process.md) |

## 正本の境界

- 思想は [`philosophy.md`](./philosophy.md) に残す。多数の既存相対リンクがあるため移動しない。
- 設計原則は [`design-principles.md`](./design-principles.md) に残す。節番号参照があるため移動しない。
- 失敗パターンの正本は [`anti-patterns.md`](./anti-patterns.md) に残す。governance 側は運用入口を置く。
- component contract は [`components/`](./components/) に残す。既存 `components/*.md` の path と節構造は凍結する。
- `workflow.md`, `architecture.md`, `data-sources.md`, `python-foundation.md` は互換 shim として旧本文を保持する。新規参照は `operations/`, `architecture/`, `reference/` の正本へ寄せる。
