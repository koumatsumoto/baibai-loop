---
title: "Architecture"
summary: "Baibai-Loop の構造の正本：3 層インフラ（データ / 決定論的分析 / 判断）と単一ループ、repository map、CLI/SQLite の安定契約。"
doc_type: architecture
status: active
last_reviewed: 2026-07-02
---

# Architecture — 構造・repository map・安定契約

Baibai-Loop の **構造** の正本。思想・大戦略は [`doctrine.md`](./doctrine.md)、資本・ポジション管理は [`portfolio-management.md`](./portfolio-management.md)、各工程の手順は [`workflow/`](./workflow/) を参照する。

日本株の実データを機械的に収集・解析・スコアリングし、割安さの機械判定を土台に長期積立の裁量判断を支える基盤。構造としては、**3 層インフラ**（データ / 決定論的分析 / 判断）の上を **1 つの長期投資ループ**が流れる。

## 3 層インフラ

| 層 | 実体 | 性質 |
| --- | --- | --- |
| L1 データ層 | `data/screening/market.sqlite`（J-Quants 価格・財務 / EDINET metrics / JPX 規制） | 全上場銘柄の再現可能な事実。coverage は fail-fast で検証 |
| L2 分析層 | screen（valuation ranking）・selection lens・軸別スコア。機械ふるいの事実出力 = `records/02-candidates/` | 決定論的・閾値固定の機械処理。出力は事実 |
| L3 判断層 | `records/`（macro context / thesis / position） | 人間 + AI 下書きの解釈と判断。見積り（RR・期待利回り）と採否を決める |

L2 の「分析」は決定論的な機械処理であり、その出力（candidates・軸別スコア）は **事実** として扱う。人間 / AI の解釈を伴う analysis（macro context・thesis）は L3。3 層の判定基準は「人間の判断が入るか」。

## 単一ループと repository のマッピング

3 層の上を、[`doctrine.md`](./doctrine.md) §2 の単一ループ（運用方針 → マクロ分析 → 割安 screening → リサーチ候補選定 → 個別調査 → 買い → 長期保有 → 割高で全売り → 見積り calibration）が流れる。各 stage の repository 上の実体：

| 日本語概念名 | slug | repository location | レイヤー | 役割 |
| --- | --- | --- | --- | --- |
| 運用方針 | portfolio management | [`docs/portfolio-management.md`](./portfolio-management.md) | governance | 資本・許容リスク・ポジション管理・kill switch |
| マクロ環境分析 | macro context | `records/01-macro-context/` | analysis | 姿勢・セクター・AI 前提の環境読み |
| 通過銘柄リスト | candidates | `records/02-candidates/`（git 外の local store） | fact | screen の生の事実出力（銘柄単位） |
| 個別銘柄リサーチ | thesis | `records/03-thesis/` | analysis | FV・RR・期待利回り・耐性・採否の投資メモ |
| 売買提案 | trade proposal | GitHub Issue（records 外） | 判断の入口 | 銘柄 / 価格 / 株数を人間に上げる |
| 売買執行記録 | position | `records/04-position/` | execution | 注文・約定・保有・全売り決済・calibration |

表は各 stage の artifact / record の repository location を示す（engine の `select` 等は L2 機械処理で record を持たない）。役割の詳細は [`doctrine.md#vocabulary`](./doctrine.md#vocabulary)。**売買提案は GitHub Issue を成果物とし、`records/` にディレクトリを持たない**。承認結果は position record に落ちる。

## スコープと非目標

- 割安な優良銘柄を **長期で積み立て**、valuation（割高化）で **全売り** する裁量支援基盤。日本の個別株のみ（ETF / 投資信託 / 海外株は扱わない）。買い建てのみ・現物のみ。
- Markdown / YAML と Git を正本にする。ただし週次 screen output（candidates YAML）は再生成可能な L2 機械出力として local store に置き git に積まない。
- 成果物の機械契約は `records/_schemas/*.json` を正本（contract-of-record）にする。
- **構造としての非目標**：MCP / API server・第三者向けサービング・部分売却 / リバランスの schema 化・**固定期間の review gate**。戦略上の非目標（ML スコアリング・短期 forward-backtest・自動発注・口座 / 税制モデル化）は [`doctrine.md`](./doctrine.md) §8 を参照。

<a id="repository-map"></a>

## Repository map

各 directory の責務。

### Root

| path | 責務 |
| --- | --- |
| `README.md` | 初見向け概要、主要 docs への入口 |
| `AGENTS.md` | AI agent 向け作業規約と self-review gate |
| `docs/` | 思想・構造・工程手順・参照情報 |
| `records/` | 運用成果物と運用支援 asset |
| `data/` | screening / macro 指標の local SQLite store（git 管理外、正本は [`reference/screening-runtime.md`](./reference/screening-runtime.md)） |
| `src/baibai_loop/` | 7 subsystem package の実装 |
| `tests/` | CLI・provider・schema・validator・position tracking の automated tests |
| `.github/` | CI、security audit、Dependabot |
| `reports/` | 工程横断の dated 分析・レポート出力（macro context ダイジェスト・銘柄選定・HTML） |
| `pyproject.toml` / `uv.lock` | Python package と dependency lock の正本 |

### Source subsystems

`src/baibai_loop/` は 7 package に分かれ、依存方向は import-linter（7 contract、`pyproject.toml [tool.importlinter]`）で固定する。

| package | 責務 | CLI |
| --- | --- | --- |
| `foundation/` | 共有 primitive（日付・env・filesystem・yaml）。他 subsystem を import しない import sink | — |
| `market/` | 価格・market calendar の data-access 層（J-Quants）。`foundation` のみに依存 | — |
| `macro/` | macro 環境分析（`context` ＋ `indicators` data 層）。screening / position / validation から独立 | `baibai-loop-macro` |
| `screening/` | universe → 機械スクリーニング（valuation ranking）→ candidates 生成、selection | `baibai-loop-screening` |
| `thesis/` | investment memo の domain engine（schema・payoff・sizing・refs）。最上位層 | （`baibai-loop-validation` 経由） |
| `position/` | trade record・保有 price tracking・benchmark-relative return | `baibai-loop-position` |
| `validation/` | records（公開言語）の検証 dispatcher。domain は entry surface 経由でのみ参照 | `baibai-loop-validation` |

依存方向は `foundation ← market ← {screening, position} ← thesis`（`A ← B` ＝「B が A を import」の向き）。`macro` は `foundation` の上に立つ **独立枝** で spine に属さず、`validation` は `thesis` / `position` を entry surface 経由で駆動する。7 contract は (1) macro 独立、(2) foundation = import sink、(3) market は foundation のみ、(4) position ↛ screening、(5) screening ↛ position、(6) thesis は最上位（下位層は thesis を import しない。thesis は screening / position を import してよい）、(7) validation は entry surface 経由のみ、を強制する。

### Records

| path | レイヤー | 責務 |
| --- | --- | --- |
| `records/01-macro-context/` | analysis | screening 前に読む macro context YAML |
| `records/02-candidates/` | fact | candidates YAML（git 追跡しない local store） |
| `records/03-thesis/` | analysis | investment memo Markdown。同一銘柄を再審査した場合は最新 record が正で、置き換えられた旧版は `records/_archive/` へ移す |
| `records/04-position/` | execution | trade record Markdown |

通常の record は出来事ごとの成果物（event artifact）として path 自体を正本にし、更新され続ける「最新一覧」の index は持たない。

### Records support areas

| path | 正本 docs | 役割 |
| --- | --- | --- |
| `records/_config/` | [`workflow/screening.md`](./workflow/screening.md) | screening rules と selection profile config |
| `records/_playbooks/` | [`workflow/playbooks.md`](./workflow/playbooks.md) | 運用中 playbook（value archetype）の保存領域 |
| `records/_schemas/` | [`reference/testing-and-validation.md`](./reference/testing-and-validation.md) | records validation schema の保存領域 |
| `records/_archive/` | 本 doc（この表） | 再審査などで置き換えられた過去 record の凍結保管。validator / select の走査対象外で、当時の contract のまま変更せず保持する |

### records/_schemas — 公開言語の kernel（contract-of-record）

`records/_schemas/*.json`（JSON Schema draft 2020-12）は records artifact（macro-context / candidates / thesis / position）の形を固定する **公開言語の中心資産**であり、成果物の機械契約の正本。CLI の YAML 出力、records front matter、validation 検証、AI が読む契約はこの schema set を共有語彙の基盤にする。doc 側は JSON に書けないもの（式・enum の意味・WHY・境界）だけを持ち、field を再転記しない。

<a id="automation"></a>

## Automation（CLI / schema / CI）

Automation は人間の投資判断を置き換えず、fact snapshot 生成・schema 検証・保有計測・CI 再現性を支える補助。

### CLI

| command | 実装領域 | 責務 |
| --- | --- | --- |
| `baibai-loop-screening bootstrap-cache --asof` | `screening/` | screening run に必要な J-Quants / EDINET / JPX window を SQLite 正本へ補完 |
| `baibai-loop-screening extract-edinet-metrics --asof` | `screening/` | EDINET CSV から TTM metrics を抽出し SQLite へ保存 |
| `baibai-loop-screening verify-cache-coverage --asof` | `screening/` | SQLite が screening run の必須入力を満たすか read-only 検証 |
| `baibai-loop-screening run --asof` | `screening/` | 完全性検証済み SQLite から candidates YAML を生成 |
| `baibai-loop-screening select --asof --macro-context` | `screening/` | candidates と macro context を突合し research 候補を triage |
| `baibai-loop-screening ticker-profile --ticker` | `screening/` | 個別銘柄の事実 packet（全上場対応） |
| `baibai-loop-screening market-snapshot` | `screening/` | regime 履歴・sector 集計（macro context の機械入力） |
| `baibai-loop-screening calibration-build --start --end` | `screening/` | 見積り較正の point-in-time 月次 panel + forward return を local store へ構築（cache のみ） |
| `baibai-loop-screening calibration-evaluate` | `screening/` | 較正 cohort の評価（rank IC / decile / selection replay / トラップ率）を YAML 出力 |
| `baibai-loop-macro` | `macro/` | 指標 series を provenance 付きで取得・cache |
| `baibai-loop-validation` | `validation/` | records と schema の整合を検証 |
| `baibai-loop-position benchmark` | `position/` | 保有の entry 以降リターンと benchmark（`1321`）比を算出 |

### Schema and validation

`records/_schemas/` が validator の input schema、`src/baibai_loop/validation/` が schema だけでは表現しにくい cross-file validation、`tests/test_validate_*.py` が validator の期待挙動を固定する（正本は [`reference/testing-and-validation.md`](./reference/testing-and-validation.md)）。

### CI

| workflow | trigger | gate |
| --- | --- | --- |
| `.github/workflows/ci.yml` | pull request / main push | `uv sync`, Ruff format/check, mypy, tracked raw screening cache block, pytest coverage, `baibai-loop-validation`, build |
| `.github/workflows/security.yml` | pull request / main push / weekly | Bandit, pip-audit |

Python runtime・dependency・quality gate の詳細は [`reference/python-foundation.md`](./reference/python-foundation.md)。

## 安定契約（platform interface）

AI / スクリプトが基盤を利用するための安定化対象は **2 面だけ**。これ以外（Python 内部 API・`.cache/` の中間物）は予告なく変わる。

- **契約 1：CLI の YAML 出力** — `run`（candidates 事実）・`select`（recommendations + diagnostics）・`ticker-profile`・`market-snapshot`・`baibai-loop-macro`。field の追加は随時、既存 field の名前と意味は黙って変えない。人間向け整形は stdout サマリに分離する。
- **契約 2：SQLite schema**（`data/screening/market.sqlite`） — 対象は全上場銘柄、`PRAGMA user_version` で版管理、破壊的変更は version bump + rebuild（migration しない）。**AI は読み取り専用で SQL を直接発行してよく、書き込みは CLI（bootstrap / extract / run）経由に限る**。主要テーブルは `jquants_daily_bars` / `jquants_fin_summaries` / `jquants_master_snapshots` / `edinet_metrics` / `jpx_regulation_flags`、定義の正本は [`reference/screening-runtime.md`](./reference/screening-runtime.md)。

AI の利用モデル：L1/L2 は SQL 直接発行と CLI 出力で自由に読み、すべての主張を SQL で検証できる事実へ遡れる形で書く（AP-01）。L3 は下書きまで（最終採用判定・failure 分類・macro 前提確認は人間）。スコアは軸別座標であり売買判定ではない。

## Docs sections

| path | 責務 |
| --- | --- |
| `doctrine.md` | 思想・大戦略・原則・語彙 |
| `architecture.md` | 構造・repository map・automation・安定契約（本 doc） |
| `portfolio-management.md` | 資本・ポジション管理・kill switch |
| `anti-patterns.md` | 失敗パターンと commit 前チェックリスト |
| `workflow/` | 単一ループ各工程の手順（macro / screening / research / position / playbooks） |
| `reference/` | valuation-metrics・screening-runtime・data-sources・python-foundation・configuration・testing-and-validation・jquants-rate-limits |
| `operations/` | 工程横断の手順（task-runbook / incident-runbook） |

## 参考

- [`doctrine.md`](./doctrine.md)：思想・大戦略・5 柱・語彙
- [`portfolio-management.md`](./portfolio-management.md)：資本・ポジション管理
- [`reference/screening-runtime.md`](./reference/screening-runtime.md)：CLI / provider / SQLite schema の実装仕様
- [`reference/testing-and-validation.md`](./reference/testing-and-validation.md)：schema / validator 境界
