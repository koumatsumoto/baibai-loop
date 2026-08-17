# stores/market

`market.sqlite` は価格・calendar・開示データを保持する store です。fetch 由来の 15 table は
R2 の L1 release が canonical で、この file はそこから満たされる runtime copy です
（`baibai-engine lake hydrate`）。取得範囲の帳簿と operator 導出 fact の 4 table だけが
ここを canonical とし、R2 が持つ copy はその 4 table だけを運びます
（[market lake](../../docs/reference/market-lake.md#daily-cutover)）。
