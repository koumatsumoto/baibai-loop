# Repository Agent Instructions

このリポジトリで `brief/` を作成・更新する前に、必ず [`docs/workflow.md`](docs/workflow.md) の「brief 作成前の欠損確認」を実行する。

- `world-weekly` の対象期間に gap がある場合、現在週を作る前に欠損週を backfill する
- `world-daily` が存在しても `world-weekly` 欠損の代替にはしない
- backfill 後、現在週の `前週 brief` と前週比計算の基準を更新する
- 欠損を放置したまま commit / PR しない
