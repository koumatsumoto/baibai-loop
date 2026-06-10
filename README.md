# Baibai-Loop

Baibai-Loop は、日本株の実データ(価格・財務・開示・規制)を機械的に収集・正規化し、固定ルールでスクリーニング・スコアリングし、その効果を forward 計測で検証し続ける**データ解析基盤**です。AI と人間はこの基盤の出力を使って銘柄リサーチと売買判断を行います。目的は「お買い得銘柄を拾い、トレード成績を最大化する」ことです。

運用の詳細は [`docs/`](./docs/) を正本とします。初めて読む場合は [`docs/README.md`](./docs/README.md) から入ってください。

## 3 層モデル

| 層 | 実体 | 性質 |
| --- | --- | --- |
| L1 データ層 | `data/screening/market.sqlite`(J-Quants 価格・財務 / EDINET metrics / JPX 規制) | 全上場銘柄の再現可能な事実 |
| L2 分析層 | screen lanes・selection lenses・軸別スコア・forward telemetry(replay / lane cohorts / ablation) | 決定論的・閾値固定の機械的分析 |
| L3 判断層 | `records/`(research / trades / reviews、macro context) | 人間 + AI 下書きの解釈と判断 |

AI が利用する安定契約は **CLI の YAML 出力と SQLite schema の 2 面**です([`docs/reference/platform-interface.md`](./docs/reference/platform-interface.md))。判断と帰責は人間(L3)に残し、AI は L1/L2 の事実に grounded な下書きを作ります。

## 目的の loop

以下の loop を forward-only に回します。概念モデルの正本は [`docs/concepts.md`](./docs/concepts.md) です。

1. portfolio policy で目的・制約・資本・許容リスクを固定する
2. スクリーニング前に macro context を確認する: `records/01-macro-context/`
3. スクリーニング基準でふるいにかける: `records/04-candidates/`
4. 個別銘柄を investment memo として深掘り調査する: `records/05-research/`
5. 条件を満たしたら execution record を残す: `records/06-trades/`
6. 事後検証と attribution で次回改善に活かす: `records/07-reviews/`

L3 の売買 record が ground truth となって L2 の計測 loop(どの screen・lens が forward return を生んだか)を閉じます。思想は [`docs/philosophy.md`](./docs/philosophy.md)、現行構造は [`docs/architecture/system-overview.md`](./docs/architecture/system-overview.md) を参照してください。

## 対象としないこと

- バックテスト最適化、パラメータ探索、playbook の過去データ fit(行うのは forward 計測のみ)
- 機械学習によるスコアリング・予測
- 自動発注、売買推奨(単一の合成スコアや売買指示は出力しない。スコアは軸別の座標であり判定ではない)
- リアルタイム処理(日次・週次バッチで足りる)
- 汎用 feature store / BI 基盤、第三者向けサービング

詳細な非バックテスト原則は [`docs/design-principles.md`](./docs/design-principles.md) §9 を参照してください。

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
│   ├── 07-reviews/
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
