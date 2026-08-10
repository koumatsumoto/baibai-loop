---
title: "空売り残高報告 L1 backfill と outcome-free sufficiency 判定"
summary: "取得可能な10年をPIT sourceへ保存したが、公式dataset floorからの連続coverageを満たさないためforward outcomeを読まずinsufficientと判定した。"
doc_type: measurement-record
status: complete
date: 2026-08-10
---

# 空売り残高報告 L1 backfill と outcome-free sufficiency 判定

価値tier: T1 — 独立需給軸の永久損失識別力を測る前に、無報告0を正しく主張できる入力coverageかを判定した。

## 結論

総合 verdict は `insufficient`。J-Quants V2 の現在契約が返す実取得floorは `2016-08-10` で、空売り残高報告datasetの公式floor `2013-11-07` に届かない。事前登録のゼロ語義はdataset floorからcohort as-ofまでの連続coverageを必須とするため、取得済み期間より後のcohortでも「報告なし」を0へ補完しない。

この判定はforward returnをjoinする前に成立した。H-1 / H-2 / H-3、control、trap、3y / 5y effectは計測していない。coverage不足を効果なしへ読み替えない。

## source 契約と実装

- 公式 ClientV2 `get_mkt_short_sale_report` / `/markets/short-sale-report` を使う。全銘柄の `from/to` は `code` 必須で HTTP 400 となるため、全銘柄backfillは `disclosed_date` の日次取得とした。
- 実responseのfieldは `DiscDate`, `CalcDate`, `Code`, `SSName`, `DICName`, `FundName`, `ShrtPosToSO`, `ShrtPosShares`, `ShrtPosUnits`, `PrevRptDate`, `PrevRptRatio`, `Notes`。long field名も同じnormalizerで受ける。
- L1 は disclosure / calculation の両日、provider row ordinal、reporter名tuple、ratio / shares / units、前回報告、取消、notesを保存する。同一reporter・同一両日に異なるratioが複数あるrowを上書きしない。
- ratioなしで取消を明示するrowは `is_cancellation = 1` とし、古いactive stateを終了させる。Pandas `NaN` / `NaT` は文字列factへ変換しない。
- 空payloadはcoverageを記録するが、既存rowがあるrangeを空payloadで置換しない。daily bootstrapは訂正取込みのため直近7日を再確認し、既存coverage内に停止期間のgapがあれば同時に修復する。明示history backfillとは分離する。
- panel readerは `disclosed_at <= asof` かつ `calculated_at <= asof` の最新stateだけを使う。同率最新stateが複数あるtickerだけをnullにする。dataset floor coverage不成立ではsnapshot全体をunavailableにする。

一次仕様:

- [J-Quants API Client for Python](https://github.com/J-Quants/jquants-api-client-python)
- [J-Quants Pro 空売り残高報告 dataset](https://pro.jpx-jquants.com/datasets/10)
- [J-Quants 個人向けプランの履歴期間](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20250505-01.html)

## backfill 結果

| 項目 | 実測 |
| --- | ---: |
| source coverage | `2016-08-10..2026-08-10`, `status: ok` |
| L1 rows | 1,410,801 |
| reportのあるdisclosure date | 2,442 |
| unique ticker | 4,112 |
| unique reporter identity | 629 |
| cancellation row | 6 |
| ratio nullかつ非取消 | 0 |
| `NaN` / `NaT` 文字列残留 | 0 |
| 2026-08-10時点の同率最新曖昧ticker | 9 |
| market store SHA-256 | `b71666989f9f3c1bd291e6fbde2dc2d03f7f5574def84726ca3ddc53a2c1def9` |

dataset floorの1日取得は、契約範囲が `2016-08-10 ~` である旨を返して失敗した。この期間を空payload成功として記録せず、coverage rowは1件の実取得範囲だけである。`read_reported_short_metrics(..., 2026-08-10)` は `None` を返すことを独立SQL集計後に確認した。

## 採否とsurface

ranking、gate、E[r]、FV、sizing、warningへの接続はない。L1 table、read-through、coverage、PIT aggregate、calibration panelのnullable raw列だけを残す。panel列は公式floor coverageが成立する環境で初めて値を返し、現在storeでは全cohortがnullとなる。

Premium等で `2013-11-07` から取得可能になった場合は、同じ事前登録のoutcome-free sufficiencyを再実行できる。floorを満たすまでforward outcomeを開かない。

## 検証

- future disclosure除外、取消state、0.5%未満終了state、複数reporter合計、coverage gap、empty replacement拒否、同率最新tickerだけのnull、NaN / NaT正規化をfixtureで固定した。
- market schema v19からv20のforward migrationとfresh schema shapeを検証した。
- CLI seamでhistory backfillとdaily bootstrapのsource呼出しを検証した。
- canonical constants `UPSIDE_CAP = 0.50`、`BUYBACK_CLIP = 0.05`、selection ranking、production gateは変更していない。
