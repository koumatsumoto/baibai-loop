# EDINET CSV Metrics Coverage

EDINET `type=5` CSV-derived metrics の coverage / precision を確認するための記録。`strict-net-cash-discount` と `fcf-yield-discount` は、hit 数だけではなく「抽出値が research で使える品質か」を受け入れ条件にする。

## 1. Source Policy

- Source: EDINET API v2 `documents.json?type=2` と `documents/{docID}?type=5`
- 対象書類: 有価証券報告書 (`120`)、訂正有価証券報告書 (`130`)、四半期報告書 (`140`)、半期報告書 (`160`)
- raw XBRL (`type=1`) 直接 parser は非スコープ。CSV-derived metrics の coverage / precision が不十分な場合に別 issue で検討する
- CSV ZIP 本体は `records/_data/cache/screening/edinet/csv_zips/` の derived cache。git 管理対象は抽出後の `records/_data/raw/screening/edinet/metrics/YYYY-MM-DD.json`

## 2. Extracted Metrics

| metric | source tag family | use |
| --- | --- | --- |
| `cash` | cash and deposits / cash and cash equivalents | net cash |
| `debt` | borrowings / bonds / lease obligations | net cash |
| `net_cash` | `cash - debt` | `strict-net-cash-discount` |
| `ocf_ttm` | operating cash flow | FCF / OCF |
| `capex_ttm` | purchase of PPE / intangible assets | FCF |
| `fcf_ttm` | `ocf_ttm - abs(capex_ttm)` | `fcf-yield-discount` |
| `operating_profit_ttm` | operating profit / operating income | EBITDA / profitability gate |
| `depreciation_and_amortization_ttm` | depreciation / amortization | EBITDA |

## 3. Validation Method

実装後の sample run では、以下を確認する。

- universe 内の EDINET selected filings 数
- metrics JSON の record 数
- `ttm_quality_net_cash != unavailable` の件数
- `ttm_quality_fcf != unavailable` の件数
- `failure_reasons` 上位件数
- `strict-net-cash-discount` / `fcf-yield-discount` の hit 数
- sample 3 銘柄以上について、会社 IR / 有報の一次資料と `cash` / `debt` / `CFO` / `capex` / `FCF` を突合

## 4. Initial Implementation Notes

CSV tag map は deliberately small に始める。tag が見つからない場合は推定で埋めず、`failure_reasons` に `tag_not_found:<metric>` を残す。非連結しか取れない場合は `non_consolidated_fallback` を残し、research で連結 / 単体の意味を確認する。

`strict-net-cash-discount` と `fcf-yield-discount` は「新しい候補を増やす」ための lane だが、目的は hit 数の最大化ではない。PER / PBR 中心では拾えなかった安さを、より実体に近い balance sheet / FCF で拾えるかを sample research で検証する。

## 5. 2026-05-01 Initial Run

Command:

```bash
uv run python -m baibai_loop.screening.cli extract-edinet-metrics --asof 2026-05-01 --lookback-days 540
uv run python -m baibai_loop.screening.cli rebuild-cache
uv run python -m baibai_loop.screening.cli run --asof 2026-05-01
```

EDINET metrics extraction:

| item | count |
| --- | ---: |
| selected filings | 3,999 |
| `net_cash` available | 3,764 |
| `fcf_ttm` available | 3,298 |
| `sales_ttm` available | 3,483 |
| `ebitda_ttm` available | 3,544 |
| `total_assets` available | 3,765 |
| `ttm_quality_net_cash = exact` | 1,290 |
| `ttm_quality_net_cash = approximated` | 2,474 |
| `ttm_quality_fcf = exact` | 1,129 |
| `ttm_quality_fcf = approximated` | 2,169 |

Top `failure_reasons`:

| reason | count | interpretation |
| --- | ---: | --- |
| `debt_assumed_zero` | 2,990 | debt line item が CSV に無く、cash が取れているため 0 として扱った。research では有利子負債を一次確認する |
| `non_consolidated_fallback` | 796 | 連結行が無く単体値で fallback |
| `tag_not_found:capex` | 701 | FCF 判定不可 |
| `tag_not_found:sales` | 516 | P/S / sales 補完不可 |
| `tag_not_found:ocf` | 312 | OCF / FCF 判定不可 |

Screening result:

| item | count |
| --- | ---: |
| universe | 1,347 |
| candidates | 409 |
| `valuation-reversion` | 100 |
| `strict-net-cash-discount` | 87 |
| `fcf-yield-discount` | 18 |
| `cash-rich-asset-discount` | 31 |
| `cashflow-yield-discount` | 183 |
| `sales-discount-growth` | 132 |

初回 run では `strict-net-cash-discount` が 87 件、`fcf-yield-discount` が 18 件出ており、Issue #87 の「PER / PBR だけでは拾えないお買い得候補」を広げる目的には接続できている。一方で `debt_assumed_zero` が多いため、strict net-cash candidate の research では有利子負債の一次確認を必須にする。FCF lane は `ttm_quality_fcf = exact` のみで hit させ、半期・四半期の単一期間値は screening 採用しない。
