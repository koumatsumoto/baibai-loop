# data/screening

screening 用 SQLite 正本を置くディレクトリです。

- 既定パス: `data/screening/market.sqlite`
- `screening run` は coverage 不足で停止します（daily_bars は行データから completeness を導出し DB を SSOT とする。その他のソースは `source_coverage` で判定）
- 実 DB ファイルは local store であり git 管理しません
- SQLite を作り直す場合は `bootstrap-cache --asof` と `extract-edinet-metrics --asof` で provider から必要 window を再取得します（[`../../docs/workflow/screening.md`](../../docs/workflow/screening.md)）
