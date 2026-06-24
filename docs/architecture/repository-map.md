---
title: "Repository map"
summary: "Responsibility map for root directories, records support areas, docs sections, source code, tests, and GitHub automation."
doc_type: architecture
status: active
last_reviewed: 2026-06-23
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
| `data/` | screening / macro 指標 の local SQLite store（`data/screening/market.sqlite` 等、git 管理外。正本は [`../screening/automation.md`](../screening/automation.md)） |
| `src/baibai_loop/` | 7 subsystem package の実装（下記 Source subsystems） |
| `tests/` | CLI、provider、schema、validator、decision sync、position tracking の automated tests |
| `.github/` | CI、security audit、Dependabot |
| `pyproject.toml` / `uv.lock` | Python package と dependency lock の正本 |

## Source subsystems

`src/baibai_loop/` は 7 つの subsystem package に分かれ、依存方向は import-linter（7 contract、`pyproject.toml [tool.importlinter]`）で固定します。サブシステム名から src / records / CLI / 品質改善計器を引く早見表は [`AGENTS.md`](../../AGENTS.md) のサブシステム索引を正本とします。

| package | 責務 | CLI |
| --- | --- | --- |
| `foundation/` | 共有 primitive（日付・env・filesystem・yaml）。他 subsystem を import しない import sink | — |
| `market/` | 価格・market calendar の data-access 層（J-Quants）。`foundation` のみに依存 | — |
| `macro/` | macro 環境分析（`context` ＋ `indicators` data 層）。screening / position / validation から独立 | `baibai-loop-macro` |
| `screening/` | universe → 機械スクリーニング → candidates 生成、selection、forward 計測 | `baibai-loop-screening` |
| `thesis/` | investment memo の domain engine（schema・preflight・payoff・sizing・refs）。最上位層 | （`baibai-loop-validation` 経由） |
| `position/` | trade record・price tracking・decision sync・benchmark-relative return | `baibai-loop-position` |
| `validation/` | records（公開言語）の検証 dispatcher。各 domain context は entry surface 経由でのみ参照 | `baibai-loop-validation` |

依存方向は `foundation ← market ← {macro, screening} ← thesis ← position` で、`validation` は `thesis` / `position` を駆動して domain を entry surface 経由でのみ読みます。7 contract は (1) macro 独立、(2) foundation = import sink、(3) market は foundation のみ、(4) position ↛ screening、(5) screening ↛ position（一方向 `market ← {screening, position}`）、(6) thesis は最上位（下位層は import しない）、(7) validation は entry surface 経由のみ、を強制します。

## Records

| path | レイヤー | 責務 |
| --- | --- | --- |
| `records/01-macro-context/` | analysis / macro | screening 前に確認する macro context YAML |
| `records/04-candidates/` | fact / security-level | candidates YAML(git 追跡しない local store。詳細は [`../components/candidates.md`](../components/candidates.md) §2) |
| `records/05-thesis/` | analysis / security-level | investment memo Markdown |
| `records/06-position/` | downstream | trade record Markdown |

### records のサイズと slim 化方針

git に載る records は軽量です（実測 2026-06: `_config` 24K, `_schemas` 32K, `_decisions` 40K, `_playbooks` 56K, `01-macro-context` 68K, `06-position` 80K, `05-thesis` 164K, repo root の `reports/` 116K）。唯一重いのは `records/04-candidates/` の週次 screen YAML（約 8MB）ですが、これは `.gitignore` の `records/04-candidates/**/*.yaml` で git 外の再生成可能 local store として除外され、git tracked はディレクトリ＋1 file のみです。重い payload は既に git 外、tracked record と `reports/` は軽量なので、records の物理 slim 化は計測上のメリットが乏しく、現状維持が妥当です。

## Records support areas

`records/_*` は運用成果物そのものではなく、生成・検証・検証後追跡を支える領域です。重複と drift を避けるため、正本 docs は 1 つに固定します。

通常 record (`01-macro-context` から `06-position`) は event artifact として path
自体を正本にし、mutable latest index は持たない。Support area の変更履歴も git に一本化し、
Record から参照する repo 内 file path を正本にする。

| path | 正本 docs | 参照 docs | 役割 |
| --- | --- | --- | --- |
| `records/_config/` | [`../screening/principles.md`](../screening/principles.md) | this map | screening rules and lightweight profile config files |
| `records/_decisions/` | [`../components/decisions.md`](../components/decisions.md) | this map | decision register の記録領域（`baibai-loop-position sync` が正規化） |
| `records/_playbooks/` | [`../components/playbooks.md`](../components/playbooks.md) | this map | 運用中 playbook の保存領域 |
| `records/_schemas/` | [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) | [`automation-map.md`](./automation-map.md) | records validation schema の保存領域 |

### records/_schemas — 公開言語の kernel

`records/_schemas/` は records artifact（macro-context / candidates / thesis / position / decision）の形を固定する JSON Schema（draft 2020-12）群であり、Baibai-Loop の**公開言語の中心資産（kernel）**です。CLI の YAML 出力、records front matter、validation 検証、AI が読む契約はすべてこの schema set を共有語彙の基盤にします。schema を変えることは公開言語そのものを変えることなので、変更は [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) と validation subsystem を正本に進めます。

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
