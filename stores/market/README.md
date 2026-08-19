# stores/market

`market.sqlite` は価格・calendar・開示データを保持する store です。lake 所有の 17 table は
R2 の L1 release が canonical で、この file はそこから満たされる runtime copy です
（`baibai-engine lake hydrate`）。取得範囲の帳簿と月次 snapshot の operator 導出 fact の
2 table だけがここを canonical とし、R2 が持つ copy はその 2 table だけを運びます
（[market lake](../../docs/reference/market-lake.md#daily-cutover)）。
