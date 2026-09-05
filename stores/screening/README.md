# stores/screening

screening run と calibration の machine estimate store を置きます。market の
L1 store は [`../market/`](../market/) が所有します。

- run store の既定パス: `stores/screening/runs.sqlite`
- `screening run` は coverage 不足で停止します（daily_bars は行データから completeness を導出し DB を SSOT とする。その他のソースは `source_coverage` で判定）
- 実 DB ファイルは git 管理しません。run storeはrebuildable L2で、cloudはoperational machine bundleを保持しますが、canonical L3 judgmentはapplication DBが所有します
- run storeとmarket storeはcurrent schemaだけを読みます。schema cutoverはmain merge後にone-shot toolで完了し、runtime migrationや旧schema readerを残しません
- 既存 row を migration で埋められない backfill は `screening invalidate-coverage --source <name> [--start --end]` で該当 `source_coverage` を消し、`bootstrap-cache --asof` で再取得します
- market SQLite はcurrent L1 releaseから`lake hydrate`し、当日入力の不足だけを`bootstrap-cache --asof`と`extract-edinet-metrics --asof`で補います（[`../../docs/reference/screening-runtime.md`](../../docs/reference/screening-runtime.md)）
