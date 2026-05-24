# Repository Agent Instructions

Baibai-Loop の運用作業を AI エージェントに任せるときの最小規約。リポジトリ全体の構造は [`README.md`](./README.md)、docs 入口は [`docs/README.md`](./docs/README.md) を読む。

## 作業前に必ず読む

- 全体構造: [`docs/architecture/system-overview.md`](./docs/architecture/system-overview.md)
- repository map: [`docs/architecture/repository-map.md`](./docs/architecture/repository-map.md)
- 思想: [`docs/philosophy.md`](./docs/philosophy.md)
- 設計原則: [`docs/design-principles.md`](./docs/design-principles.md)
- 運用手順入口: [`docs/operations/README.md`](./docs/operations/README.md)
- 触る成分の仕様: [`docs/components/`](./docs/components/)
- data sources / validation / Python 基盤: [`docs/reference/README.md`](./docs/reference/README.md)
- **失敗パターンと再発防止**: [`docs/anti-patterns.md`](./docs/anti-patterns.md) — 過去の PR レビューで繰り返し指摘された類型集。macro context / research / validator を編集する前に該当節のチェックリストを 1 周すること

## 言語運用

人間向けの運用記録、調査メモ、作業メモ、最終報告は原則日本語で書く。ただし、schema field、ticker、tool output、コード/API 名、固有の英語指標名は自然に英語のままでよい。

## commit 前 / PR 前の self-review

records / src / docs の変更を含む commit を作る前に、[`docs/anti-patterns.md`](./docs/anti-patterns.md) の対応する anti-pattern (AP-01〜AP-09) のチェックリストを通過させること。特に以下は 100% 防ぐ:

- 一次情報を直接確認せず二次情報・推測で書く (AP-01)
- 数値計算を機械的に検算しない (AP-02)
- 株価異常値の corporate action 確認を skip する (AP-03)
- schema / 実装の意味を読まずに推測で解釈する (AP-04)
- macro context の根拠 URL / series / used_for を曖昧にする
- 公表日 / source の最新性確認を skip する (AP-07)
- validator の抜け道を意識しない (AP-08)
- 外部 AI 分析や system output を override せず records に取り込む / paper proxy と実資金集中度を混同する / 注文と約定の状態を区別しない (AP-09)

成分別の詳細チェックリスト:
- macro context 編集時: [`docs/components/macro-context.md`](./docs/components/macro-context.md)
- research 編集時: [`docs/components/research.md`](./docs/components/research.md) §8.1

メタ運用 (失敗パターンの再発防止):
- 同じ failure mode を 2 回以上 PR review で指摘されたら、[`docs/anti-patterns.md`](./docs/anti-patterns.md) の該当節を強化する
- 新 validator rule を追加するときは、anti-patterns.md AP-08 のチェックリストを必ず更新して次回 review で同じ穴が再発しないように記録する
- 一次情報 (Tier 1) が継続的に取得困難な指標は [`docs/reference/data-sources.md`](./docs/reference/data-sources.md) §「一次統計の数値で Tier 1 取得が困難な場合の Tier 2 例外運用」に従い、`status: failed` Tier 1 と `status: ok` Tier 2 を併記する
- Python 構文を review で指摘する前に、必ず [`pyproject.toml`](./pyproject.toml) の `requires-python` / Ruff `target-version` と [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) §3 を確認する。この repo は Python 3.14 固定だが、Ruff は `target-version = "py313"` にして PEP 758 の `except T1, T2:` へ自動整形されないようにしている。複数例外捕捉は必ず `except (T1, T2):` と書く

## 事実と分析の分離

`records/04-candidates/` は事実層、`records/01-macro-context/` と `records/05-research/` は分析層。事実ファイルに解釈・予測・相場観を書かない。詳細は [`docs/design-principles.md`](./docs/design-principles.md)。

## 検証

records / schema の変更を加えたら、コミット前に最低限以下を通す。

```bash
uv run baibai-loop-validate
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI と同じ手順は [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) §9 を参照。
