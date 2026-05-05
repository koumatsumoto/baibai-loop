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
| metrics records | 3,999 |
| `net_cash` available | 3,205 |
| `fcf_ttm` available | 3,295 |
| `sales_ttm` available | 3,476 |
| `ebitda_ttm` available | 3,538 |
| `total_assets` available | 3,753 |
| `ttm_quality_net_cash = exact` | 825 |
| `ttm_quality_net_cash = approximated` | 2,380 |
| `ttm_quality_net_cash = unavailable` | 794 |
| `ttm_quality_fcf = exact` | 829 |
| `ttm_quality_fcf = approximated` | 2,466 |
| `ttm_quality_fcf = unavailable` | 704 |

Top `failure_reasons`:

| reason | count | interpretation |
| --- | ---: | --- |
| `non_consolidated_fallback` | 799 | 連結行が無く単体値で fallback |
| `tag_not_found:capex` | 704 | FCF 判定不可 |
| `debt_assumed_zero` | 547 | cash は取れたが debt element が見つからない。strict net-cash 判定では使わない |
| `tag_not_found:sales` | 523 | P/S / sales 補完不可 |
| `tag_not_found:ocf` | 313 | OCF / FCF 判定不可 |
| `tag_not_found:cash` | 247 | net cash 判定不可 |
| `tag_not_found:debt` | 246 | net cash 判定不可 |

Screening result:

| item | count |
| --- | ---: |
| universe | 1,347 |
| candidates | 370 |
| candidates vs pre-EDINET #87 sample | +23 |
| new tickers vs pre-EDINET #87 sample | 23 |
| `valuation-reversion` | 94 |
| `strict-net-cash-discount` | 20 |
| `strict-net-cash-discount` new vs pre-EDINET #87 sample | 12 |
| `strict-net-cash-discount` exclusive | 12 |
| `strict-net-cash-discount` with `debt_assumed_zero` | 0 |
| `fcf-yield-discount` | 14 |
| `fcf-yield-discount` new vs pre-EDINET #87 sample | 2 |
| `fcf-yield-discount` exclusive | 1 |
| `cash-rich-asset-discount` | 31 |
| `cashflow-yield-discount` | 183 |
| `sales-discount-growth` | 132 |

Document selection audit:

| item | count |
| --- | ---: |
| old correction overriding newer different-period filing | 0 |

Initial PR #93 実装では `strict-net-cash-discount` が 87 件出ていたが、そのうち 61 件が `debt_assumed_zero` に依存していた。これは strict lane として false positive risk が高いため、debt element が見つからない銘柄は strict 判定から除外した。修正後は strict hit が 20 件へ減ったが、`debt_assumed_zero` 依存は 0 件になった。候補数を増やすよりも、research 工数を投じる価値のある net-cash 候補に絞る判断である。

FCF lane は `ttm_quality_fcf = exact` のみで hit させ、半期・四半期の単一期間値は screening 採用しない。修正後は 14 件 hit、既存候補外 2 件、exclusive 1 件で、現時点の独自発見力は限定的。ただし OCF だけでは設備投資負担を見誤るため、FCF が exact に取れる銘柄の quality lane として残す。

Candidate sanity check:

| ticker | observation |
| --- | --- |
| 4008 | 初期 PR #93 では debt 抽出漏れにより strict hit していたが、`ShortTermLoansPayable` 等を debt に含めた後は候補外。false positive 削減として妥当 |
| 3151 | 初期 PR #93 では old correction / debt 抽出漏れの影響を受けていたが、修正後は候補外 |
| 9682 | 引き続き `sales-discount-growth` 候補。EDINET metrics は `debt_assumed_zero` のため strict net-cash thesis は強めない。FCF yield も 2.7% 程度で FCF lane には不十分 |
