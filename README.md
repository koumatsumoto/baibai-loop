# Baibai-Loop

Baibai-Loop は、日本株スイングトレードの戦略立案、スクリーニング、売買実行、事後検証を一貫して記録し、継続的に改善するためのリポジトリです。

運用の詳細は [`docs/`](./docs/) を正本とします。初めて読む場合は [`docs/README.md`](./docs/README.md) から入ってください。

## 目的

以下の loop を forward-only に回します。

1. マクロ事実を蓄積する: `records/01-brief/`
2. マクロ見解を更新する: `records/02-outlook/`
3. スクリーニング基準でふるいにかける: `records/03-candidates/`
4. 個別銘柄を深掘り調査する: `records/04-research/`
5. 条件を満たしたら取引する: `records/05-trades/`
6. 事後検証と retro で次回改善に活かす: `records/06-reviews/`

思想は [`docs/philosophy.md`](./docs/philosophy.md)、現行構造は [`docs/architecture/system-overview.md`](./docs/architecture/system-overview.md) を参照してください。

## 対象としないこと

本リポジトリは裁量トレーダー向けの decision-support 基盤です。以下は行いません。

- バックテスト、累積リターン計算、パラメータ最適化
- 自動発注
- 戦略 performance claim
- 未来情報を含む historical simulation
- playbook の過去データ fit

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
│   ├── 01-brief/
│   ├── 02-outlook/
│   ├── 03-candidates/
│   ├── 04-research/
│   ├── 05-trades/
│   ├── 06-reviews/
│   ├── _data/
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
| 現行アーキテクチャ | [`docs/architecture/README.md`](./docs/architecture/README.md) |
| component contract | [`docs/components/README.md`](./docs/components/README.md) |
| 運用 runbook | [`docs/operations/README.md`](./docs/operations/README.md) |
| data sources / validation / Python 基盤 | [`docs/reference/README.md`](./docs/reference/README.md) |
| docs governance / ADR | [`docs/governance/README.md`](./docs/governance/README.md) |
| screening subsystem | [`docs/screening/README.md`](./docs/screening/README.md) |
| templates | [`docs/templates/README.md`](./docs/templates/README.md) |

## CLI

```bash
uv run baibai-loop-screening run --asof YYYY-MM-DD
uv run baibai-loop-screening select --asof YYYY-MM-DD
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
