---
playbook_id: cash-rich-asset-discount
signal_lane: cash-rich-asset-discount
status: active
---

# Cash-Rich Asset Discount

## Purpose

J-Quants financial summary の `CashEq` と `Eq` を使い、現金・純資産に対して時価総額が安い候補を扱う。これは厳密 net cash 判定ではない。

EDINET `net_cash_to_market_cap` が取得できる場合は、J-Quants CashEq proxy より EDINET net cash を優先して contradiction check を行う。EDINET 上で net debt と分かる銘柄は、CashEq / market cap が高くてもこの evidence path から除外する。

## Entry Focus

- `cash_to_market_cap`、`price_to_equity`、`equity_ratio` が screening rule を満たす。
- EDINET `net_cash_to_market_cap` が取得できる場合、設定下限を下回っていない。
- 営業赤字ではない。
- cash が有利子負債や運転資金に食われるだけではないことを一次情報で確認する。

## Required Research Checks

- Cash / equity snapshot。
- 有利子負債確認。
- 現金の使途。
- 資本効率と資本政策。
- Shareholder return check。
- Entry / Exit / Invalidation。
- Position size。
