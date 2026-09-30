---
title: "信用取引残高の公表制度変更"
summary: "2026-09-28の公表制度変更に伴うsource境界、取込、公表ラグの契約。"
doc_type: reference
status: active
---

# 信用取引残高の公表制度変更契約（2026-09-28）

## 1. 目的

JPX の信用取引残高は 2026-09-28 に公表粒度が変わる。L1 store は変更前後のデータを別 table・
別 balance date 域として保持し、指標側は「どちらの series がその balance date を公表したか」を
明示して読む。本書は公表日、balance date、対象母集団を区別する取込契約の正本である。

本書は§6の固定語義と制度切替後のsource契約を所有する。初回契約確認と連続性の実測は
[当時の記録](../../reports/operations/2026-09-28-margin-publication-transition/)を参照する。

## 2. 公式日程

JPX の集計システムは 2026-09-27 に移行し、移行可否は同日 20:00 に公表される。移行が
実施され、変更後データの初回は 2026-09-25 残高を 2026-09-28 16:00 に公表した。
変更前の最終週次データは 2026-09-18 残高を 2026-09-25 に公表した。残高対象日と公表日は区別する。

- JPX 2026-07-06 告知: <https://www.jpx.co.jp/news/1032/20260706-01.html>
- 変更内容: <https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rk9-att/t13vrt000000chxp.pdf>
- 変更前後の公表日程: <https://www.jpx.co.jp/news/1032/t13vrt000001iuz1-att/t13vrt000001iv2u.pdf>
- J-Quants Pro 全銘柄 daily dataset: <https://pro.jpx-jquants.com/datasets/3>
- J-Quants Pro 日々公表銘柄等 dataset: <https://pro.jpx-jquants.com/datasets/2>

## 3. source 対応表

| source / table | 対象 | 日付 identity | cadence / 公表 | 利用契約 |
| --- | --- | --- | --- | --- |
| J-Quants `margin-interest` / `jquants_weekly_margin` | 全銘柄 | `week_end` | 2026-09-18 残高まで週次。原則第2営業日に公表 | 2026-09-18 残高までの `margin_*` の残高 source。9/18 後を拒否する |
| J-Quants `margin-alert` / `jquants_margin_alerts` | 日々公表銘柄等だけ | `(publication_date, ticker)` | 日次。現行 API は同日 16:30 更新 | 過熱・規制 risk の fact / annotation 用。全銘柄系列や rank / gate へ代用しない |
| J-Quants `margin-interest` 全銘柄 daily / `jquants_all_issues_daily_margin` | 全銘柄 | `(balance_date, ticker)` | 2026-09-25 残高から日次。翌営業日 16:00 公表 | 2026-09-25 残高以後の `margin_*` の残高 source。ClientV2 bindingは2026-09-28に実payloadを確認して有効化した（[検証記録](../../reports/operations/2026-09-28-margin-publication-transition/contract.md)）。週次 table と row を union しない |

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
- 週次残高は第2取引日の公表が到来した候補だけをdaily bootstrapが取得する。公表前または公表日の
  途中に取得したempty snapshotは最終的な「非公表週」とみなさず、公表後のbatchで一度再取得する。
  公表後にもemptyだった週はcleanな対象なしとして保持し、以後は再取得しない。
- daily batch は `margin-alert` の直近 7 日を再取得し、遅延追加・訂正を取り込む。
- 最終週次 2026-09-18 残高は 2026-09-25 以後、clean かつ non-empty な snapshot を
  保存するまで再取得する。公表前の空 response を既存 cache が保持していても、最終週を
  欠損させないためである。
- 2026-09-25 以後の全銘柄daily残高は、翌営業日の公表後に通常経路で取り込み・利用する。
  daily batch は stored trading days から次の営業日が到来済みのbalance dateだけを要求する。
- 全銘柄日次の empty response は coverage 完了とみなさず、non-empty な clean snapshot を
  保存するまで次の batch で再取得する。
- J-Quants client/API が field、endpoint、日付 identity、母集団を変更した場合は取込を停止し、
  本表と fixture を更新してから再開する。既存 table への推測 mapping は行わない。

## 6. `margin_*` の固定語義

`margin_long_to_adv`、`margin_short_to_adv`、`margin_long_share`、
`margin_long_delta_26w`、`margin_std_long_share` は「公表済みの直近残高」から導出する。
残高を公表する series は 2026-09-18 で入れ替わるので、balance date が source を決める —
`week_end <= 2026-09-18` は `jquants_weekly_margin`、`balance_date >= 2026-09-25` は
`jquants_all_issues_daily_margin` である。

**軸の語義は cadence で変わらない。** level 軸は直近公表残高であり、日次化は解像度の向上で
あって意味の変更ではない。`margin_long_delta_26w` は 26 週前との比較であり、日次側からも各
ISO 週の最終 balance date だけを sampling して週間隔を保つ — 日次行を 26 個遡ると半年の変化が
5 週間の変化に化ける。

日次の高頻度性そのものを使う軸（日次 delta など）はこの列へ足さない。別名の metric と事前登録
study を用意する。

serving の統合列は `sqlite_reader.published_margin_balance_dates` で、公表ラグは cadence 別に
持つ（週次は第 2 営業日、日次は翌営業日）。
