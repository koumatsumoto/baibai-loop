# data/screening

screening 用 SQLite 正本を置くディレクトリです。

- 既定パス: `data/screening/market.sqlite`
- `screening run` はこの SQLite の `source_coverage` と各正規化 table が不足している場合に停止します
- 実 DB ファイルは local store であり git 管理しません
- legacy raw JSON からの初回移行が必要な場合は `docs/screening/automation.md` の `rebuild-cache` 手順を参照します
