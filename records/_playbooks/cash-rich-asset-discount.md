---
playbook_id: cash-rich-asset-discount
signal_lane: cash-rich-asset-discount
status: active
---

# Cash-Rich Asset Discount

## Purpose

J-Quants financial summary の `CashEq` と `Eq` を使い、現金・純資産に対して時価総額が安い候補を扱う。これは厳密 net cash 判定ではない。

## Entry Focus

- `cash_to_market_cap` と `price_to_equity` が screening rule を満たす。
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
