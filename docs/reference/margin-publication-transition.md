# 信用取引残高の公表制度変更契約（2026-09-28）

## 1. 目的

JPX の信用取引残高は 2026-09-28 に公表粒度が変わる。L1 store は変更前後のデータを別の
語義として保持し、週次由来の screening / calibration 指標へ日次値を混入させない。本書は
公表日、balance date、対象母集団を区別する取込契約の正本である。

## 2. 公式日程

JPX の集計システムは 2026-09-27 に移行し、移行可否は同日 20:00 に公表される。移行が
実施された場合、変更後データの初回は 2026-09-25 残高を 2026-09-28 16:00 に公表する。
変更前の最終データは 2026-09-18 残高を 2026-09-24 に公表する。

- JPX 2026-07-06 告知: <https://www.jpx.co.jp/news/1032/20260706-01.html>
- 変更内容: <https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rk9-att/t13vrt000000chxp.pdf>
- 変更前後の公表日程: <https://www.jpx.co.jp/news/1032/t13vrt000001iuz1-att/t13vrt000001iv2u.pdf>
- J-Quants Pro 全銘柄 daily dataset: <https://pro.jpx-jquants.com/datasets/3>
- J-Quants Pro 日々公表銘柄等 dataset: <https://pro.jpx-jquants.com/datasets/2>

## 3. source 対応表

| source / table | 対象 | 日付 identity | cadence / 公表 | 利用契約 |
| --- | --- | --- | --- | --- |
| J-Quants `margin-interest` / `jquants_weekly_margin` | 全銘柄 | `week_end` | 2026-09-18 残高まで週次。原則第2営業日に公表 | 既存 `margin_*` の唯一の残高 source。9/18 後を拒否する |
| J-Quants `margin-alert` / `jquants_margin_alerts` | 日々公表銘柄等だけ | `(publication_date, ticker)` | 日次。現行 API は同日 16:30 更新 | 過熱・規制 risk の fact / annotation 用。全銘柄系列や rank / gate へ代用しない |
| J-Quants Pro 全銘柄 daily / `jquants_all_issues_daily_margin` | 全銘柄 | `(balance_date, ticker)` | 2026-09-25 残高から日次。翌営業日 16:00 公表 | 日次系列専用。ClientV2 binding は U4 で実 payload を確認するまで無効。週次 table と既存 `margin_*` へ接続しない |

`margin-alert` は `PubDate` と `AppDate` を別々に保存する。`TSEMrgnRegCls` は取引所の規制
分類 fact であり、残高値から導出しない。全銘柄日次系列は `Date` を balance date として保存し、
公表前の当日残高を要求しない。

## 4. field 対応

全銘柄日次系列の `LongVol` / `ShrtVol` / standard / negotiable 内訳 / `IssType` は、同名の
週次 payload field と同じ物理量でも cadence が異なる。したがって別 table に保存し、週次 row と
union しない。6 残高 field は有限・非負、`IssType` は non-null を clean snapshot の必須条件とし、
一つでも欠けた response は partial coverage として再取得対象に残す。

日々公表銘柄等は `ShrtOut` / `LongOut`、各 change / ratio、standard / negotiable 内訳、
`PubReason`、`TSEMrgnRegCls` を provider field のまま L1 列へ写す。この dataset は対象銘柄が
選別されるため、欠けている ticker を残高ゼロと解釈しない。

## 5. 連続性と発火条件

- `jquants_weekly_margin` は `week_end <= 2026-09-18`、全銘柄日次 table は
  `balance_date >= 2026-09-25` を application と SQLite `CHECK` の両方で強制する。境界違反は
  provider ingest、直接 SQL、store merge のいずれでも fail-fast する。
- daily batch は `margin-alert` の直近 7 日を再取得し、遅延追加・訂正を取り込む。
- 最終週次 2026-09-18 残高は 2026-09-24 以後、clean かつ non-empty な snapshot を
  保存するまで再取得する。公表前の空 response を既存 cache が保持していても、最終週を
  欠損させないためである。
- 2026-09-28 以後、daily batch は stored trading days から「次の営業日が到来済み」の
  balance date だけを要求する。最初の対象は 2026-09-25 である。ただし calendar は移行実施の
  authority ではないため、`ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED` は初期値 `False` とする。
- U4 は JPX の移行実施告知と、repository が利用する J-Quants ClientV2 の endpoint / field /
  date identity / 母集団を実 payload で確認する。両方が一致した commit だけが activation flag を
  `True` にする。移行中止または ClientV2 未対応なら無効のままにする。
- 全銘柄日次の empty response は coverage 完了とみなさず、non-empty な clean snapshot を
  保存するまで次の batch で再取得する。
- J-Quants client/API が field、endpoint、日付 identity、母集団を変更した場合は取込を停止し、
  本表と fixture を更新してから再開する。既存 table への推測 mapping は行わない。
- 9/28 の移行中止告知が出た場合、全銘柄日次取込を開始しない。JPX の新しい実施日程を確認し、
  境界定数と fixture を同時に更新する。

## 6. `margin_*` の固定語義

`margin_long_to_adv`、`margin_short_to_adv`、`margin_long_share`、
`margin_long_delta_26w`、`margin_std_long_share` はすべて公表済みの
`jquants_weekly_margin` 週末残高から導出する。全銘柄日次系列を同じ列へ流し込まない。
日次軸を評価する場合は、別名の metric と事前登録 study を用意する。
