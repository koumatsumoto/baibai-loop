---
title: "Repository map"
summary: "Responsibility map for root directories, records support areas, docs sections, source code, tests, and GitHub automation."
doc_type: architecture
status: active
last_reviewed: 2026-05-04
source_paths:
  - "../../records/"
  - "../../src/baibai_loop/"
  - "../../tests/"
  - "../../.github/"
---

# Repository map

この map は現在の repo 構造の正本説明です。移行履歴は扱わず、各 directory の責務だけを示します。

## Root

| path | 責務 |
| --- | --- |
| `README.md` | 初見向け概要、最短の構成図、主要 docs への入口 |
| `AGENTS.md` | AI agent 向け作業規約と self-review gate |
| `docs/` | 仕様、運用手順、参照情報、docs governance |
| `records/` | 運用成果物と運用支援 asset |
| `src/baibai_loop/` | screening、validation、ledger sync などの CLI 実装 |
| `tests/` | CLI、provider、schema、validator、ledger の automated tests |
| `.github/` | CI、security audit、Dependabot、ledger sync workflow |
| `pyproject.toml` / `uv.lock` | Python package と dependency lock の正本 |

## Records

| path | レイヤー | 責務 |
| --- | --- | --- |
| `records/01-policy/` | policy | portfolio policy files |
| `records/02-brief/` | fact / macro | brief YAML |
| `records/03-outlook/` | analysis / macro | outlook YAML |
| `records/04-candidates/` | fact / security-level | candidates YAML |
| `records/05-research/` | analysis / security-level | investment memo Markdown |
| `records/06-trades/` | downstream | trade record Markdown |
| `records/07-reviews/` | downstream | individual review と monthly retro |

## Records support areas

`records/_*` は運用成果物そのものではなく、生成・検証・検証後追跡を支える領域です。重複と drift を避けるため、正本 docs は 1 つに固定します。

通常 record (`01-policy` を除く `02-brief` から `07-reviews`) は event artifact として path
自体を正本にし、mutable latest index は持たない。Support area も `_changelog.jsonl` や
content hash audit は持たず、record から参照する repo 内 file path と git 履歴を正本にする。

| path | 正本 docs | 参照 docs | 役割 |
| --- | --- | --- | --- |
| `records/_approval-rules/` | [`../components/research.md`](../components/research.md) | this map | analyst asserted evidence を sizing に入れる approval rule files |
| `records/_benchmarks/` | [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) | [`automation-map.md`](./automation-map.md) | business regression benchmark manifest |
| `records/_calendars/` | [`../components/portfolio-policy.md`](../components/portfolio-policy.md) | this map | business day / event / corporate action calendar files |
| `records/_config/` | [`../screening/principles.md`](../screening/principles.md) | this map | screening rules / metric catalog / exposure bucket config files |
| `records/_data/` | [`../reference/data-sources.md`](../reference/data-sources.md) | this map | raw / derived data と cache の支援領域 |
| `records/_ledger/` | [`../components/ledger.md`](../components/ledger.md) | this map | decision register と ledger sync の記録領域 |
| `records/_market-data/` | [`../components/reviews.md`](../components/reviews.md) | this map | review / missed opportunity 計算用 market data files |
| `records/_playbooks/` | [`../components/playbooks.md`](../components/playbooks.md) | this map | 運用中 playbook の保存領域 |
| `records/_portfolio-exposure/` | [`../components/trades.md`](../components/trades.md) | this map | order intent / exposure cap 検査用 portfolio exposure files |
| `records/_schemas/` | [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) | [`automation-map.md`](./automation-map.md) | records validation schema の保存領域 |
| `records/_universe-snapshots/` | [`../components/candidates.md`](../components/candidates.md) | this map | screening universe files |

## Docs sections

| path | 責務 |
| --- | --- |
| `docs/architecture/` | 現行構造、情報フロー、repository map、automation map、ADR |
| `docs/components/` | 成果物ごとの contract。既存 `components/*.md` は path と節構造を凍結 |
| `docs/operations/` | 運用 runbook の入口。component docs から本文を移動しない |
| `docs/reference/` | data sources、configuration、layout、testing、Python foundation |
| `docs/glossary/` | 金融一般の意味と Baibai-Loop 固有語 |
| `docs/governance/` | docs style、review process、ADR process、anti-pattern 運用 |
| `docs/screening/` | candidates 生成 subsystem の詳細 |
| `docs/templates/` | artifact 作成用 template |
