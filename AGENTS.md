# Repository Agent Instructions

Baibai-Loop の運用作業を AI エージェントに任せるときの最小規約。リポジトリ全体の構造は [`README.md`](./README.md)、docs 入口は [`docs/README.md`](./docs/README.md) を読む。

## 作業前に必ず読む

- 思想・大戦略・語彙: [`docs/doctrine.md`](./docs/doctrine.md)
- 構造・repository map・CLI/SQLite 契約: [`docs/architecture.md`](./docs/architecture.md)
- 資本・ポジション管理: [`docs/portfolio-management.md`](./docs/portfolio-management.md)
- 各工程の手順: [`docs/workflow/README.md`](./docs/workflow/README.md)
- data sources / validation / Python 基盤: [`docs/reference/README.md`](./docs/reference/README.md)
- **失敗パターンと再発防止**: [`docs/anti-patterns.md`](./docs/anti-patterns.md) — 過去の PR レビューで繰り返し指摘された類型集。macro context / thesis / validator を編集する前に該当節のチェックリストを 1 周すること

## サブシステム索引

サブシステム名（macro / screening / thesis / position など）を指定されたら、この表で src / records / CLI / 品質改善計器を引いて着手する。各工程の詳細は [`docs/workflow/`](./docs/workflow/)、依存構造（7 package・7 import-linter contract）は [`docs/architecture.md#repository-map`](./docs/architecture.md#repository-map) を正本とする。

| subsystem | src | records | CLI | 品質改善計器 |
| --- | --- | --- | --- | --- |
| macro | `src/baibai_loop/macro/` | `records/01-macro-context/` | `baibai-loop-macro` | 見積り calibration（[`workflow/macro.md`](./docs/workflow/macro.md)、formal loop にしない） |
| screening | `src/baibai_loop/screening/` | `records/04-candidates/`, `records/_config/` | `baibai-loop-screening` | 見積り calibration（保有 outcome。短期 backtest はしない） |
| thesis | `src/baibai_loop/thesis/` | `records/05-thesis/`, `records/_playbooks/` | （`baibai-loop-validation --target thesis` 経由） | preflight gate（`thesis/preflight.py`） |
| position | `src/baibai_loop/position/` | `records/06-position/` | `baibai-loop-position` | 見積り calibration（entry 見積り vs 実現） |
| market | `src/baibai_loop/market/` | （`data/screening/market.sqlite` ほか、git 外） | — | 価格・calendar data 層（screening・保有計測の価格基盤） |
| foundation | `src/baibai_loop/foundation/` | — | — | 共有 primitive（import sink、固有の計器なし） |
| validation | `src/baibai_loop/validation/` | `records/_schemas/`（検証対象 schema） | `baibai-loop-validation` | records 公開言語の検証器（CI gate） |

品質改善は単一の見積り calibration に集約する: entry 時の見積り（RR・期待利回り・FV）を保有の実現結果と突き合わせ、macro 読み・screening 閾値・FV 推定・耐性判定を離散的に改善する（短期 universe backtest はしない）。thesis は発注前の preflight gate を持つ。詳細は各 [`docs/workflow/`](./docs/workflow/) doc を正本とする。

## 言語運用

人間向けの運用記録、調査メモ、作業メモ、最終報告は原則日本語で書く。ただし、schema field、ticker、tool output、コード/API 名、固有の英語指標名は自然に英語のままでよい。

## commit 前 / PR 前の self-review

records / src / docs の変更を含む commit を作る前に、[`docs/anti-patterns.md`](./docs/anti-patterns.md) の対応する anti-pattern (AP-01〜AP-10) のチェックリストを通過させること。特に以下は 100% 防ぐ:

- 一次情報を直接確認せず二次情報・推測で書く (AP-01)
- 数値計算を機械的に検算しない (AP-02)
- 株価異常値の corporate action 確認を skip する (AP-03)
- schema / 実装の意味を読まずに推測で解釈する (AP-04)
- macro context の根拠 URL / series / used_for を曖昧にする
- 公表日 / source の最新性確認を skip する (AP-07)
- validator の抜け道を意識しない (AP-08)
- 外部 AI 分析や system output を override せず records に取り込む / 集中度の分母を単一プール `real_capital_yen` 簿価で評価しない / 注文と約定の状態を区別しない (AP-09)

成分別の詳細チェックリスト:
- macro context 編集時: [`docs/workflow/macro.md`](./docs/workflow/macro.md)
- thesis 編集時: [`docs/workflow/research.md`](./docs/workflow/research.md)

メタ運用 (失敗パターンの再発防止):
- 同じ failure mode を 2 回以上 PR review で指摘されたら、[`docs/anti-patterns.md`](./docs/anti-patterns.md) の該当節を強化する
- 新 validator rule を追加するときは、anti-patterns.md AP-08 のチェックリストを必ず更新して次回 review で同じ穴が再発しないように記録する
- 一次情報 (Tier 1) が継続的に取得困難な指標は [`docs/reference/data-sources.md`](./docs/reference/data-sources.md) §「一次統計の数値で Tier 1 取得が困難な場合の Tier 2 例外運用」に従い、`status: failed` Tier 1 と `status: ok` Tier 2 を併記する
- Python 構文を review で指摘する前に、必ず [`pyproject.toml`](./pyproject.toml) の `requires-python` / Ruff `target-version` と [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) §3 を確認する。この repo は Python 3.14 固定だが、Ruff は `target-version = "py313"` にして PEP 758 の `except T1, T2:` へ自動整形されないようにしている。複数例外捕捉は必ず `except (T1, T2):` と書く

## 銘柄提示時の TradingView リンク

このリポジトリで ticker（証券コード）を提案・提示するときは、必ず TradingView チャート URL `https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A<code>`（`:` は `%3A`）を Markdown リンクで併記する。WSL / Windows では `powershell.exe -NoProfile -Command "Start-Process '<url>'"` で既定ブラウザにも開く（`explorer.exe` / `cmd start` は query 付き URL を壊すので使わない）。focus 銘柄（最終候補・推し）は自動で開き、screening の大量一覧はリンクは全件・ブラウザ起動は focus 分だけにする。手順の詳細は [`.claude/skills/tradingview-open/SKILL.md`](./.claude/skills/tradingview-open/SKILL.md)。

## 事実と分析の分離

`records/04-candidates/` は事実層、`records/01-macro-context/` と `records/05-thesis/` は分析層。事実ファイルに解釈・予測・相場観を書かない。詳細は [`docs/doctrine.md#fact-analysis-separation`](./docs/doctrine.md#fact-analysis-separation)。

## 検証

records / schema の変更を加えたら、コミット前に最低限以下を通す。

```bash
uv run baibai-loop-validation
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI と同じ手順は [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) §9 を参照。
