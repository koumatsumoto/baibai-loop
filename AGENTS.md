# Repository Agent Instructions

Baibai-Loop の運用作業を AI エージェントに任せるときの最小規約。リポジトリ全体の構造は [`README.md`](./README.md)、設計の根拠は [`docs/`](./docs/) を読む。

## 作業前に必ず読む

- 全体構造: [`docs/architecture.md`](./docs/architecture.md)
- 思想: [`docs/philosophy.md`](./docs/philosophy.md)
- 設計原則: [`docs/design-principles.md`](./docs/design-principles.md)
- 運用手順: [`docs/workflow.md`](./docs/workflow.md)
- 触る成分の仕様: [`docs/components/`](./docs/components/)

## 事実と分析の分離

`records/01-brief/` と `records/03-candidates/` は事実層、`records/02-outlook/` と `records/04-research/` は分析層。事実ファイルに解釈・予測・相場観を書かない。詳細は [`docs/design-principles.md`](./docs/design-principles.md)。

## brief 作成前の欠損確認

`records/01-brief/` の `world-weekly` / `world-daily` / `macro-monthly` を新規作成・更新する前に、必ず [`docs/workflow.md`](./docs/workflow.md) の「brief 作成前の欠損確認」を実行する。

- `world-weekly` の対象期間に gap がある場合、現在週を作る前に欠損週を backfill する
- `world-daily` が存在しても `world-weekly` 欠損の代替にはしない
- backfill 後、現在週の `references.prev_period` と前週比計算の基準を更新する
- 欠損を放置したまま commit / PR しない

## 検証

records / schema の変更を加えたら、コミット前に最低限以下を通す。

```bash
uv run baibai-loop-validate
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI と同じ手順は [`docs/python-foundation.md`](./docs/python-foundation.md) §9 を参照。
