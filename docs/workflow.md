# 世界情勢調査の運用ルール

Baibai-Loop における世界情勢・相場環境の調査記録に関する最小限の運用ルール。

## 更新頻度

- **週次レギュラー**: 毎週 1 回、前週の経済指標・マーケット動向をまとめる
- **イベント時臨時**: FOMC / 日銀金融政策決定会合 / 主要指標発表時などの重要イベントで都度追加する

## ファイル配置と命名

```
journal/YYYY/MM/YYYY-MM-DD-{kind}-{slug}.md
```

- `{kind}` は `world-weekly` / `fomc` / `boj` / `cpi` / `gdp` / `geopolitics` などイベント種別を示す
- `{slug}` は内容を端的に示す短い英小文字ハイフン区切り文字列（例: `us-cpi-beat`）
- INDEX ファイルは作らない。一覧は `git ls-files journal/` または GitHub 上のツリーで確認する

## データソースと引用

- 使うソースは [docs/data-sources.md](./data-sources.md) に限定する
- 引用は本文中にインラインで `[ソース名](URL) (YYYY-MM-DD取得)` の形式を使う
- 数値や事実はできるだけ Tier 1 から取り、Tier 2 は一次統計で拾えない事象に限定する

## テンプレート

- 新しい記録を作るときは [docs/templates/world-analysis.md](./templates/world-analysis.md) をコピーして使う
- 事実ベース運用のため、テンプレートに主観的な「解釈」「示唆」欄は設けていない
