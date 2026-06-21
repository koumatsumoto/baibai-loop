# Baibai-Loop

Baibai-Loop は、日本株の実データ(価格・財務・開示・規制)を機械的に収集・正規化し、固定ルールでスクリーニング・スコアリングし、その効果を forward 計測で検証し続ける**データ解析基盤**です。AI と人間はこの基盤の出力を使って銘柄リサーチと売買判断を行います。目的は「お買い得銘柄を拾い、トレード成績を最大化する」ことです。

運用の詳細は [`docs/`](./docs/) を正本とします。初めて読む場合は [`docs/README.md`](./docs/README.md) から入ってください。

## 3 層モデル

| 層 | 実体 | 性質 |
| --- | --- | --- |
| L1 データ層 | `data/screening/market.sqlite`(J-Quants 価格・財務 / EDINET metrics / JPX 規制) | 全上場銘柄の再現可能な事実 |
| L2 分析層 | screen lanes・selection lenses・軸別スコア・forward backtest(replay / lane cohorts / ablation) | 決定論的・閾値固定の機械的分析 |
| L3 判断層 | `records/`(research / trades、macro context) + `reports/` | 人間 + AI 下書きの解釈と判断 |

AI が利用する安定契約は **CLI の YAML 出力と SQLite schema の 2 面**です([`docs/reference/platform-interface.md`](./docs/reference/platform-interface.md))。判断と帰責は人間(L3)に残し、AI は L1/L2 の事実に grounded な下書きを作ります。

## 2 つのループ

Baibai-Loop は単一ループではなく、**運用ループ**（検証済みの screening を適用して売買判断に落とす）と **改善ループ**（その screening を検証・改善する）を分けて回します。概念モデルの正本は [`docs/concepts.md`](./docs/concepts.md) です。

**運用ループ（機会/週次）**:

1. portfolio policy で目的・制約・資本・許容リスクを固定する
2. スクリーニング前に macro context を確認する: `records/01-macro-context/`
3. スクリーニング基準でふるいにかける: `records/04-candidates/`
4. 個別銘柄を investment memo として深掘り調査する: `records/05-research/`
5. 最終選考の銘柄を「いくらで何株」の売買提案として GitHub Issue に上げ、人間が判断する
6. 約定したら execution record を残す: `records/06-trades/`

**改善ループ（日次/週次）**: screening が機能しているかは、母数が極小の個人売買結果ではなく **全候補の forward-only backtest(大 N)**で検証します。改善項目は GitHub Issue の改善バックログで管理し、screening rules / playbooks の改訂に落とします。trades は Q2(執行品質)の信号として還流します。思想は [`docs/philosophy.md`](./docs/philosophy.md)、現行構造は [`docs/architecture/system-overview.md`](./docs/architecture/system-overview.md) を参照してください。

## 対象としないこと

- 過去データへの閾値 grid search / パラメータ最適化、戦略累積リターン(年率・MaxDD・シャープ)の track-record claim(screening 効果の検証は forward-only な multi-axis backtest で行う。[`docs/operations/backtest-runbook.md`](./docs/operations/backtest-runbook.md))
- 機械学習によるスコアリング・予測
- 自動発注、売買推奨(単一の合成スコアや売買指示は出力しない。スコアは軸別の座標であり判定ではない)
- リアルタイム処理(日次・週次バッチで足りる)
- 汎用 feature store / BI 基盤、第三者向けサービング

計測の原則(forward-only な backtest と、避ける最適化)は [`docs/design-principles.md`](./docs/design-principles.md) §9 を参照してください。

## 構成

```text
baibai-loop/
├── README.md
├── AGENTS.md
├── docs/
│   ├── README.md
│   ├── architecture/
│   ├── components/
│   ├── operations/
│   ├── reference/
│   ├── governance/
│   ├── screening/
│   └── templates/
├── records/
│   ├── 01-macro-context/
│   ├── 04-candidates/
│   ├── 05-research/
│   ├── 06-trades/
│   ├── _config/
│   ├── _ledger/
│   ├── _playbooks/
│   └── _schemas/
├── src/baibai_loop/
├── tests/
├── .github/
├── pyproject.toml
└── uv.lock
```

directory ごとの責務は [`docs/architecture/repository-map.md`](./docs/architecture/repository-map.md) を参照してください。

## 主要 docs

| 目的 | doc |
| --- | --- |
| docs portal | [`docs/README.md`](./docs/README.md) |
| 概念モデル / 用語 | [`docs/concepts.md`](./docs/concepts.md) |
| 現行アーキテクチャ | [`docs/architecture/README.md`](./docs/architecture/README.md) |
| component contract | [`docs/components/README.md`](./docs/components/README.md) |
| 運用 runbook | [`docs/operations/README.md`](./docs/operations/README.md) |
| data sources / validation / Python 基盤 | [`docs/reference/README.md`](./docs/reference/README.md) |
| anti-pattern 運用 | [`docs/governance/README.md`](./docs/governance/README.md) |
| screening subsystem | [`docs/screening/README.md`](./docs/screening/README.md) |
| templates | [`docs/templates/README.md`](./docs/templates/README.md) |

## CLI

```bash
uv run baibai-loop-screening run --asof YYYY-MM-DD
uv run baibai-loop-screening select --asof YYYY-MM-DD --macro-context records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-slug.yaml
uv run baibai-loop-stats search CPI
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```

automation の位置付けは [`docs/architecture/automation-map.md`](./docs/architecture/automation-map.md)、validation は [`docs/reference/testing-and-validation.md`](./docs/reference/testing-and-validation.md) を参照してください。

## 開発と検証

```bash
uv run baibai-loop-validate
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI と local parity の詳細は [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) を参照してください。

## 改善バックログ

改善点・未解決の設計課題は GitHub Issues で管理します。

<https://github.com/koumatsumoto/baibai-loop/issues>
