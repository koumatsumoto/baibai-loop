---
title: "System overview"
summary: "Baibai-Loop architecture: the 3-layer infrastructure (data / deterministic analysis / judgment) and the two loops (operating, improvement) that run on it."
doc_type: architecture
status: active
last_reviewed: 2026-06-21
related_docs:
  - "../concepts.md"
  - "../philosophy.md"
  - "../design-principles.md"
---

# System overview

Baibai-Loop は、日本株の実データを機械的に収集・解析・スコアリングし、その効果を forward-only な backtest で検証し続けるデータ解析基盤です。構造は **3 層インフラ**（データ / 決定論的分析 / 判断）の上で、**2 つのループ**（運用ループ・改善ループ）が動きます。正準の概念モデルとループ詳細は [`../concepts.md`](../concepts.md) を正本とし、この doc は repository path とインフラ層を併記します。

## 3 層インフラ

| 層 | 実体 | 性質 |
| --- | --- | --- |
| L1 データ層 | `data/screening/market.sqlite`(J-Quants 価格・財務 / EDINET metrics / JPX 規制) | 全上場銘柄の再現可能な事実。coverage は fail-fast で検証する |
| L2 分析層 | screen playbooks([`../screening/mechanical.md`](../screening/mechanical.md))・selection lenses・軸別スコア・forward backtest(replay / playbook cohorts / ablation)。機械ふるいの事実出力 = `records/04-candidates/` | 決定論的・閾値固定の機械処理。すべて forward 計測に接続する([`../screening/extending.md`](../screening/extending.md)) |
| L3 判断層 | `records/`(research / trades、macro context) + `reports/` (forward 計測の dated まとめ) | 人間 + AI 下書きの解釈と判断。**screening 効果の検証は全候補 backtest（改善ループ・大 N）が担い、trades は Q2 執行品質の信号を供給する** |

AI / スクリプトが利用する安定契約は CLI YAML 出力と SQLite schema の 2 面([`../reference/platform-interface.md`](../reference/platform-interface.md))。L2 の「分析」は決定論的な機械処理であり、その出力(candidates・backtest 数値)は事実として扱います。人間 / AI の解釈を伴う analysis レイヤー(macro context、investment memo、reports)は L3 に属します。3 層と 2 ループの判定基準は「人間の判断が入るか」です。

| 日本語概念名 | Concept (slug) | Repository location | レイヤー | 役割 |
| --- | --- | --- | --- | --- |
| 運用方針 | portfolio policy | [`docs/portfolio-policy.md`](../portfolio-policy.md) | governance | 目的、制約、資本、許容リスク、time horizon を固定する |
| マクロ環境分析 | macro context | `records/01-macro-context/` | analysis | 外部記事と統計 series を参照し、screening 前の市場環境を判断する |
| 通過銘柄リスト | candidates（screen output） | `records/04-candidates/` | fact | universe と screening rule から ticker-level raw screen output を記録する |
| 個別銘柄リサーチ | research（investment memo） | `records/05-thesis/` | analysis | candidates と macro context を統合し、thesis payoff と採用可否を判断する |
| 売買提案 | trade proposal（GitHub Issue） | （Issue・records 外） | 判断の入口 | 最終選考銘柄の詳細 ＋ 銘柄/価格/株数 提案を人間に上げる |
| 売買執行記録 | trades（execution record） | `records/06-position/` | execution | 実際に order / entry した採用判断の注文、約定、建玉、決済を記録する |

## 2 つのループ

3 層インフラの上で、性質の違う 2 つのループが動きます（中心モデル・カデンス・分離理由は [`../concepts.md`](../concepts.md)）。

- **運用ループ（機会/週次）**: `運用方針 → マクロ環境分析 → 機械スクリーニング → 通過銘柄リスト → リサーチ候補選定 → 個別銘柄リサーチ → 売買提案(GitHub Issue) → 〔人間判断〕→ 売買執行記録`。検証済みの screening system を適用して具体的な売買判断に落とす。screening 論理は改善しない。採用可否と sizing cap は portfolio policy と research 判断が担う。Macro context は hard gate ではなく screening 前提と sector 優先度を整理する入力。
- **改善ループ（日次/週次）**: 全候補 forward-only backtest（主軸・大 N）＋ 設計レビュー（仮説源）＋ trades（Q2 執行信号）→ **GitHub Issue の改善バックログ** → screening rules / playbooks / config の改訂(PR)。screening 効果の検証主軸は backtest であり、個人の実トレード(極小 N)ではない。forward 計測の正本は [`../operations/backtest-runbook.md`](../operations/backtest-runbook.md)。

両ループの接合点は「検証済みの screening system(rules / playbooks / config)」ただ 1 点です。

## スコープ

- 基本は 2 か月以内、5-40 営業日のスイングトレードを対象にする。ただし、短期 thesis が外れた場合に長期保有へ切り替えられる銘柄を優先する policy を持つ。これは主戦略の holding period を延ばすためではなく、含み損時に損失確定を急がず、資産ロックを受け入れて回収を待てる selection principle である。
- long-only の裁量支援基盤として扱う。
- Macro context は hard gate ではなく、screening / research の確認観点として扱う。
- Markdown / YAML と Git を正本にする。ただし週次 screen output(candidates YAML)は再生成可能な L2 機械出力として local store に置き、git には積まない([`../components/candidates.md`](../components/candidates.md) §2)。
- 売買提案は GitHub Issue を成果物とし、records/ にディレクトリを持たない。承認結果は ledger と trades record に落とす。
- AI 下書きと人間確認を前提に、事実層と分析層を物理的に分ける。
- CLI は screening、selection、validation、ledger sync、forward backtest(replay / playbook cohorts / ablation)、macro statistics 取得に使う。

## 非目標

- 過去データへの閾値 grid search / パラメータ最適化、戦略累積リターンの track-record claim は行わない。screening 効果は forward-only な multi-axis backtest(記録済み output の replay / playbook-cohorts / ablation、[`../operations/backtest-runbook.md`](../operations/backtest-runbook.md) の 7 axis)で計測する。
- 機械学習によるスコアリング・予測は行わない。スコアは軸別の座標として出し、単一の合成点や売買指示には畳まない。
- 自動発注、リアルタイム処理は行わない。
- screening 閾値や playbook の **値そのもの** を過去データに fit させない(値は原則ベースで固定し、backtest は仕組みの効果計測に使う)。
- 配当利回り単独の playbook / Rerating Book は対象外にする。配当・自己株買いは、long-hold fallback 時の資産ロック中に収益が見込める preference として research で確認する。
- 汎用 feature store / BI 基盤、MCP / API server などのサービング層は導入しない。SQLite は market data の local canonical store として使い、AI は CLI と SQL で直接読む。

詳細な rationale は [`../philosophy.md`](../philosophy.md)、実践ルールは [`../design-principles.md`](../design-principles.md)、成果物ごとの contract は [`../components/README.md`](../components/README.md) を参照します。
