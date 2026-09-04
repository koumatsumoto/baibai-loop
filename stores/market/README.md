# stores/market

`market.sqlite` は価格・calendar・開示データを保持する store です。lake 所有tableは
R2 の L1 release が canonical で、この file はそこから満たされる runtime copy です
（`baibai-engine lake hydrate`）。取得範囲の帳簿`source_coverage`と月次 snapshot の operator 導出 fact
`tse_capital_policy_snapshots`はここを canonical とし、R2 の market store copy はこれらと
`lake_store_origin` metadataを運びます
（[market lake](../../docs/reference/market-lake.md#daily-cutover)）。
現行のdataset inventoryは`baibai-engine lake inventory`で確認します。
