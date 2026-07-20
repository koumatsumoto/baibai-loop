# data/screening

screening 用 SQLite 正本を置くディレクトリです。

- 既定パス: `data/screening/market.sqlite`
- `screening run` は coverage 不足で停止します（daily_bars は行データから completeness を導出し DB を SSOT とする。その他のソースは `source_coverage` で判定）
- 実 DB ファイルは local store であり git 管理しません
- schema は v13 を初期基準とする forward-only migration で進化します。schema bump 時は既存 store を再取得なしで in-place に前進 migrate し、v13 未満・最新超だけを「削除して再取得」で fail-fast します
- 既存 row を migration で埋められない backfill は `screening invalidate-coverage --source <name> [--start --end]` で該当 `source_coverage` を消し、`bootstrap-cache --asof` で再取得します
- SQLite を作り直す場合は `bootstrap-cache --asof` と `extract-edinet-metrics --asof` で provider から必要 window を再取得します（[`../../docs/workflow/screening.md`](../../docs/workflow/screening.md)）
