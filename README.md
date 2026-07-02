# Baibai-Loop

Baibai-Loop は、日本株の実データ(価格・財務・開示・規制)を機械的に収集・正規化し、固定ルールで**割安な銘柄を機械抽出**し、深い個別調査で**フェアバリュー・リスクリワード・期待利回りを見積もる**データ解析基盤です。AI と人間はこの基盤の出力を使って、割安な優良銘柄を長期で積み立て、割高化で売る判断を行います。目的は「お買い得な優良銘柄を長期で拾い、資産を積み上げる」ことです。

運用の詳細は [`docs/`](./docs/) を正本とします。初めて読む場合は [`docs/doctrine.md`](./docs/doctrine.md) → [`docs/README.md`](./docs/README.md) から入ってください。

## 3 層モデル

| 層 | 実体 | 性質 |
| --- | --- | --- |
| L1 データ層 | `data/screening/market.sqlite`(J-Quants 価格・財務 / EDINET metrics / JPX 規制) | 全上場銘柄の再現可能な事実 |
| L2 分析層 | 割安 valuation ranking・selection lens・軸別スコア | 決定論的・閾値固定の機械的分析 |
| L3 判断層 | `records/`(macro context / thesis / position) + `reports/` | 人間 + AI 下書きの解釈と判断 |

AI が利用する安定契約は **CLI の YAML 出力と SQLite schema の 2 面**です([`docs/architecture.md`](./docs/architecture.md))。判断と帰責は人間(L3)に残し、AI は L1/L2 の事実に grounded な下書きを作ります。

## 単一ループ

Baibai-Loop は、割安な優良銘柄を長期で積み立てる 1 つの投資ループを回し、その中核スキル(リスクリワード・期待利回りの見積り)を実現結果と突き合わせて継続改善します。思想の正本は [`docs/doctrine.md`](./docs/doctrine.md)。

1. 運用方針で資本・許容リスク・ポジション管理を固定する
2. マクロ分析で姿勢(ディフェンシブ / リスクオン)とセクター・AI 前提を読む: `records/01-macro-context/`
3. 割安 screening でふるいにかける: `records/04-candidates/`
4. 深い個別調査でフェアバリュー・リスクリワード・期待利回りを見積もり、塩漬け耐性を確認する: `records/05-thesis/`
5. 採用銘柄を「いくらで何株」の売買提案として GitHub Issue に上げ、人間が判断する
6. 約定したら執行記録を残し、割高化・事業毀損で全売りする: `records/06-position/`
7. 見積りと実現結果を突き合わせて較正し、次の見積りを磨く

## 対象としないこと

- 過去データへの閾値 grid search / パラメータ最適化、戦略累積リターン(年率・MaxDD・シャープ)の track-record claim
- 銘柄全体を対象にした短期 forward-backtest による screen 最適化
- 機械学習によるスコアリング・予測(単一の合成スコアや売買指示は出力しない。スコアは軸別の座標であり判定ではない)
- 自動発注、リアルタイム処理
- ETF / 投信 / 海外株、口座・税制のモデル化
- 汎用 feature store / MCP / API server

原則は [`docs/doctrine.md`](./docs/doctrine.md)、構造は [`docs/architecture.md`](./docs/architecture.md) を参照してください。

## 構成

```text
baibai-loop/
├── README.md
├── AGENTS.md
├── docs/
│   ├── doctrine.md              思想・大戦略・原則・語彙
│   ├── architecture.md          構造・repository map・CLI/SQLite 契約
│   ├── portfolio-management.md  資本・ポジション管理
│   ├── anti-patterns.md         失敗パターン
│   ├── workflow/                単一ループ各工程の手順
│   ├── reference/               valuation-metrics・screening-runtime・data/Python 基盤
│   └── operations/              工程横断の手順(task / incident)
├── records/
│   ├── 01-macro-context/
│   ├── 04-candidates/
│   ├── 05-thesis/
│   ├── 06-position/
│   ├── _config/
│   ├── _playbooks/
│   └── _schemas/
├── src/baibai_loop/
├── tests/
├── .github/
├── pyproject.toml
└── uv.lock
```

directory ごとの責務は [`docs/architecture.md#repository-map`](./docs/architecture.md#repository-map) を参照してください。

## 主要 docs

| 目的 | doc |
| --- | --- |
| docs portal | [`docs/README.md`](./docs/README.md) |
| 思想・大戦略・語彙 | [`docs/doctrine.md`](./docs/doctrine.md) |
| 構造・repository map・CLI/SQLite 契約 | [`docs/architecture.md`](./docs/architecture.md) |
| 資本・ポジション管理 | [`docs/portfolio-management.md`](./docs/portfolio-management.md) |
| 各工程の手順 | [`docs/workflow/README.md`](./docs/workflow/README.md) |
| data sources / validation / Python 基盤 | [`docs/reference/README.md`](./docs/reference/README.md) |
| 失敗パターン | [`docs/anti-patterns.md`](./docs/anti-patterns.md) |

## CLI

```bash
uv run baibai-loop-screening run --asof YYYY-MM-DD
uv run baibai-loop-screening select --asof YYYY-MM-DD --macro-context records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-slug.yaml
uv run baibai-loop-macro search CPI
uv run baibai-loop-validation
uv run baibai-loop-position benchmark
```

automation の位置付けは [`docs/architecture.md#automation`](./docs/architecture.md#automation) を参照してください。

## 開発と検証

```bash
uv run baibai-loop-validation
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI と local parity の詳細は [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) を参照してください。

## 改善バックログ

改善点・未解決の設計課題は GitHub Issues で管理します。

<https://github.com/koumatsumoto/baibai-loop/issues>
