---
title: "Repository map"
summary: "Responsibility map for root directories, records support areas, docs sections, source code, tests, and GitHub automation."
doc_type: architecture
status: active
last_reviewed: 2026-05-30
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
| `data/` | screening / macro stats の local SQLite store（`data/screening/market.sqlite` 等、git 管理外。正本は [`../screening/automation.md`](../screening/automation.md)） |
| `src/baibai_loop/` | screening、validation、ledger sync などの CLI 実装 |
| `tests/` | CLI、provider、schema、validator、ledger の automated tests |
| `.github/` | CI、security audit、Dependabot |
| `pyproject.toml` / `uv.lock` | Python package と dependency lock の正本 |

## Records

| path | レイヤー | 責務 |
| --- | --- | --- |
| `records/01-macro-context/` | analysis / macro | screening 前に確認する macro context YAML |
| `records/04-candidates/` | fact / security-level | candidates YAML(git 追跡しない local store。詳細は [`../components/candidates.md`](../components/candidates.md) §2) |
| `records/05-research/` | analysis / security-level | investment memo Markdown |
| `records/06-trades/` | downstream | trade record Markdown |
| `records/07-reviews/` | downstream | individual review と monthly retro |

## Records support areas

`records/_*` は運用成果物そのものではなく、生成・検証・検証後追跡を支える領域です。重複と drift を避けるため、正本 docs は 1 つに固定します。

通常 record (`01-macro-context` から `07-reviews`) は event artifact として path
自体を正本にし、mutable latest index は持たない。Support area の変更履歴も git に一本化し、
Record から参照する repo 内 file path を正本にする。

| path | 正本 docs | 参照 docs | 役割 |
| --- | --- | --- | --- |
| `records/_config/` | [`../screening/principles.md`](../screening/principles.md) | this map | screening rules and lightweight profile config files |
| `records/_ledger/` | [`../components/ledger.md`](../components/ledger.md) | this map | decision register と ledger sync の記録領域 |
| `records/_playbooks/` | [`../components/playbooks.md`](../components/playbooks.md) | this map | 運用中 playbook の保存領域 |
| `records/_schemas/` | [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) | [`automation-map.md`](./automation-map.md) | records validation schema の保存領域 |

## Docs sections

| path | 責務 |
| --- | --- |
| `docs/architecture/` | 現行構造、情報フロー、repository map、automation map、ADR |
| `docs/components/` | 成果物ごとの contract |
| `docs/operations/` | 運用 runbook の入口。component docs から本文を移動しない |
| `docs/reference/` | data sources、configuration、layout、testing、Python foundation |
| `docs/governance/` | anti-pattern 運用の governance 入口 |
| `docs/screening/` | candidates 生成 subsystem の詳細 |
| `docs/templates/` | artifact 作成用 template |
