# EDINET CSV Metrics Coverage

EDINET `type=5` CSV-derived metrics の coverage / precision を確認するための記録。`strict-net-cash-discount` と `fcf-yield-discount` は、hit 数だけではなく「抽出値が research で使える品質か」を受け入れ条件にする。

## 1. Source Policy

- Source: EDINET API v2 `documents.json?type=2` と `documents/{docID}?type=5`
- 対象書類: 有価証券報告書 (`120`)、訂正有価証券報告書 (`130`)、四半期報告書 (`140`)、訂正四半期報告書 (`150`)、半期報告書 (`160`)、訂正半期報告書 (`170`)
- raw XBRL (`type=1`) 直接 parser は非スコープ。CSV-derived metrics の coverage / precision が不十分な場合に別 issue で検討する
- CSV ZIP 本体は `.cache/screening/edinet/csv_zips/` の disposable cache。抽出後の metrics は `data/screening/market.sqlite` の `edinet_metrics` と `source_coverage` に保存する
- 抽出 metric には `source_doc_id`、`document_type`、`source_submit_datetime`、`source_period_start`、`source_period_end` を保持する。research ではこの metadata から対象 EDINET 書類と対象期間へ戻って一次確認する。`source_period_start/end` は EDINET documents metadata 上の書類対象期間であり、半期報告書では metric の測定期間そのものとは限らない

## 2. Extracted Metrics

| metric | source tag family | use |
| --- | --- | --- |
| `cash` | cash and deposits / cash and cash equivalents | net cash |
| `debt` | borrowings / bonds / lease obligations | net cash |
| `net_cash` | `cash - debt` | `strict-net-cash-discount` |
| `ocf_ttm` | operating cash flow | EDINET raw metric. Candidate YAML では `edinet_ocf_ttm` として出力し、J-Quants `ocf_ttm` と区別する |
| `capex_ttm` | purchase of PPE / intangible assets | FCF |
| `fcf_ttm` | `edinet_ocf_ttm - abs(capex_ttm)` | `fcf-yield-discount` |
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
- sample 3 銘柄以上について、generated artifact 上で source-consistency を確認する
- EDINET lane の top candidate について、research packet を 1 件以上作成し、screening が PER/PBR 以外の割安候補を拾えているか確認する

## 4. Initial Implementation Notes

CSV tag map は deliberately small に始める。tag が見つからない場合は推定で埋めず、`failure_reasons` に `tag_not_found:<metric>` を残す。非連結しか取れない場合は `non_consolidated_fallback` を残し、research で連結 / 単体の意味を確認する。

`strict-net-cash-discount` と `fcf-yield-discount` は「新しい候補を増やす」ための lane だが、目的は hit 数の最大化ではない。PER / PBR 中心では拾えなかった安さを、より実体に近い balance sheet / FCF で拾えるかを research で検証する。

## 5. 2026-05-01 Initial Run

Command:

```bash
uv run python -m baibai_loop.screening.cli extract-edinet-metrics --asof 2026-05-01 --lookback-days 540
uv run python -m baibai_loop.screening.cli verify-cache-coverage --asof 2026-05-01
uv run python -m baibai_loop.screening.cli run --asof 2026-05-01
```

EDINET metrics extraction:

| item | count |
| --- | ---: |
| selected filings | 3,999 |
| metrics records | 3,999 |
| selected correction filings (`130` / `150` / `170`) | 88 |
| `net_cash` available | 3,204 |
| `fcf_ttm` available | 3,295 |
| `sales_ttm` available | 3,476 |
| `ebitda_ttm` available | 3,538 |
| `total_assets` available | 3,753 |
| `ttm_quality_net_cash = exact` | 823 |
| `ttm_quality_net_cash = approximated` | 2,381 |
| `ttm_quality_net_cash = unavailable` | 795 |
| `ttm_quality_fcf = exact` | 827 |
| `ttm_quality_fcf = approximated` | 2,468 |
| `ttm_quality_fcf = unavailable` | 704 |
| source submit datetime available | 3,999 |
| source period start/end available | 3,999 |

Top `failure_reasons`:

| reason | count | interpretation |
| --- | ---: | --- |
| `non_consolidated_fallback` | 799 | 連結行が無く単体値で fallback |
| `tag_not_found:capex` | 704 | FCF 判定不可 |
| `debt_assumed_zero` | 548 | cash は取れたが debt element が見つからない。strict net-cash 判定では使わない |
| `tag_not_found:sales` | 523 | P/S / sales 補完不可 |
| `tag_not_found:ocf` | 313 | OCF / FCF 判定不可 |
| `tag_not_found:cash` | 247 | net cash 判定不可 |
| `tag_not_found:debt` | 246 | net cash 判定不可 |

Screening result:

| item | count |
| --- | ---: |
| universe | 1,347 |
| candidates | 366 |
| candidates vs pre-EDINET #87 sample | +19 |
| `valuation-reversion` | 94 |
| `strict-net-cash-discount` | 20 |
| `strict-net-cash-discount` new vs pre-EDINET #87 sample | 12 |
| `strict-net-cash-discount` exclusive | 12 |
| `strict-net-cash-discount` with `debt_assumed_zero` | 0 |
| `fcf-yield-discount` | 14 |
| `fcf-yield-discount` new vs pre-EDINET #87 sample | 2 |
| `fcf-yield-discount` exclusive | 1 |
| `cash-rich-asset-discount` | 19 |
| `cash-rich-asset-discount` with EDINET negative net cash | 0 |
| `cashflow-yield-discount` | 183 |
| `sales-discount-growth` | 132 |
| candidates using correction filing (`130` / `150` / `170`) | 7 |
| candidates with `edinet_source_period_end` after submit date | 257 |
| candidates with non-positive EV/EBITDA value | 0 |
| valuation-reversion hits using non-positive EV/EBITDA | 0 |

Document selection audit:

| item | count |
| --- | ---: |
| selected `130` correction filings | 41 |
| selected `150` correction filings | 0 |
| selected `170` correction filings | 47 |
| same-period annual corrections ignored against normal annual (`130` vs `120`) | 0 |
| same-period quarterly corrections ignored against normal quarterly (`150` vs `140`) | 0 |
| same-period semiannual corrections ignored against normal semiannual (`170` vs `160`) | 0 |
| old correction overriding newer different-period filing | 0 |

EDINET documents API の訂正書は `periodStart` / `periodEnd` が欠損しやすいため、`docDescription` の `YYYY/MM/DD－YYYY/MM/DD` 形式から期間を fallback parse する。API field がある場合は API field を優先する。document selection は period end / period start を submit time より先に比較し、古い期間の訂正書が新しい半期 / 年次の通常書類を上書きしないようにする。同一期間では最新 submit time を優先し、同一 submit time の tie-break として訂正書を通常書類より優先する。

Initial PR #93 実装では `strict-net-cash-discount` が 87 件出ていたが、そのうち 61 件が `debt_assumed_zero` に依存していた。これは strict lane として false positive risk が高いため、debt element が見つからない銘柄は strict 判定から除外した。修正後は strict hit が 20 件へ減ったが、`debt_assumed_zero` 依存は 0 件になった。候補数を増やすよりも、research 工数を投じる価値のある net-cash 候補に絞る判断である。

FCF lane は `ttm_quality_fcf = exact` のみで hit させ、半期・四半期の単一期間値は screening 採用しない。修正後は 14 件 hit、既存候補外 2 件、exclusive 1 件で、現時点の独自発見力は限定的。ただし OCF だけでは設備投資負担を見誤るため、FCF が exact に取れる銘柄の quality lane として残す。

Cash-rich lane は J-Quants CashEq proxy のため、EDINET net cash が取得できる場合は contradiction guard を入れる。2026-05-01 sample では guard 前に cash-rich 31 件のうち 16 件が EDINET `net_cash_to_market_cap < 0` だった。修正後は cash-rich 19 件、EDINET negative net cash は 0 件。候補数は減るが、目的は「現金が厚く見えるが実は net debt」の候補を research 上位から落とし、本当にお買い得な balance sheet / FCF 候補へ工数を寄せることである。

Source period caveat:

| item | count |
| --- | ---: |
| candidates with EDINET source period end after submit date | 257 |
| by document type `160` | 256 |
| by document type `170` | 1 |

これは parser の日付逆転ではなく、EDINET documents metadata が半期報告書でも fiscal year period を返すために起きる。`edinet_source_period_start/end` は source traceability と document selection のための metadata として扱い、research では書類本文の BS 基準日 / CF 測定期間を一次確認する。

OCF reconciliation:

| item | count |
| --- | ---: |
| candidates with both J-Quants `ocf_ttm` and EDINET `edinet_ocf_ttm` | 190 |
| all document types: absolute diff > 20% | 116 |
| all document types: absolute diff > 50% | 97 |
| annual / corrected annual (`120` / `130`) overlap | 63 |
| annual / corrected annual: absolute diff > 20% | 2 |
| annual / corrected annual: absolute diff > 50% | 1 |

半期 / 四半期 source を含む単純比較では差分が大きく見えるため、J-Quants OCF と EDINET OCF を横断して機械的に reconcile しない。`fcf-yield-discount` は EDINET CFO / capex / FCF を同一 source family として扱い、J-Quants `ocf_ttm` は `cashflow-yield-discount` 専用に残す。年次書類に限定すると乖離は大幅に減るため、FCF lane の exact 年次候補では EDINET source の利用価値がある。

EDINET lane research validation:

| ticker | lane | conclusion |
| --- | --- | --- |
| 7613 | `fcf-yield-discount` | `fcf_yield` 35.8%、EDINET CFO 26,539 百万円、capex 3,376 百万円、FCF 23,163 百万円。PER / PBR ではなく FCF で割安に見える候補として investment memo を追加。ただし net debt、売上 YoY マイナス、2025/12 期純利益減少があるため `research_decision.outcome: deferred` |

Candidate sanity check:

| ticker | observation |
| --- | --- |
| 4008 | 初期 PR #93 では debt 抽出漏れにより strict hit していたが、`ShortTermLoansPayable` 等を debt に含めた後は候補外。false positive 削減として妥当 |
| 3151 | 初期 PR #93 では old correction / debt 抽出漏れの影響を受けていたが、修正後は候補外 |
| 9682 | 引き続き `sales-discount-growth` 候補。EDINET metrics は `debt_assumed_zero` のため strict net-cash thesis は強めない。FCF yield も 2.7% 程度で FCF lane には不十分 |
| 6232 | 初期 PR #93 の review で、負の EV/EBITDA が valuation-reversion として拾われるリスクが見つかった。修正後は EV / EBITDA のどちらかがゼロ以下なら EV/EBITDA を `null` とし、2026-05-01 sample では候補外 |

Source-consistency audit:

| ticker | lane | generated artifact check | result |
| --- | --- | --- | --- |
| 4768 | `fcf-yield-discount` | EDINET `edinet_ocf_ttm` 92,218,000,000 - `capex_ttm` 4,372,000,000 = `fcf_ttm` 87,846,000,000。J-Quants `ocf_ttm` 115,478,000,000 は separate metric として保持 | pass |
| 7279 | `strict-net-cash-discount` | `cash` 57,666,000,000 - `debt` 8,939,000,000 = `net_cash` 48,727,000,000。`failure_reasons` なし | pass |
| 7871 | `strict-net-cash-discount` | `cash` 13,674,000,000 - `debt` 1,053,000,000 = `net_cash` 12,621,000,000。`failure_reasons` なし | pass |

この audit は parser output と candidates YAML の内部整合性確認であり、会社公表資料の表示科目との突合ではない。Research では各 playbook の required checks に従い、一次資料で CFO / capex / cash / debt の意味を再確認する。
