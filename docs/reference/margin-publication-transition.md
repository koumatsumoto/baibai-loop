# 信用取引残高の公表制度変更契約（2026-09-28）

## 1. 目的

JPX の信用取引残高は 2026-09-28 に公表粒度が変わる。L1 store は変更前後のデータを別 table・
別 balance date 域として保持し、指標側は「どちらの series がその balance date を公表したか」を
明示して読む。本書は公表日、balance date、対象母集団を区別する取込契約の正本である。

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
| J-Quants `margin-interest` / `jquants_weekly_margin` | 全銘柄 | `week_end` | 2026-09-18 残高まで週次。原則第2営業日に公表 | 2026-09-18 残高までの `margin_*` の残高 source。9/18 後を拒否する |
| J-Quants `margin-alert` / `jquants_margin_alerts` | 日々公表銘柄等だけ | `(publication_date, ticker)` | 日次。現行 API は同日 16:30 更新 | 過熱・規制 risk の fact / annotation 用。全銘柄系列や rank / gate へ代用しない |
| J-Quants Pro 全銘柄 daily / `jquants_all_issues_daily_margin` | 全銘柄 | `(balance_date, ticker)` | 2026-09-25 残高から日次。翌営業日 16:00 公表 | 2026-09-25 残高以後の `margin_*` の残高 source。ClientV2 binding は U4 で実 payload を確認するまで無効で、activation までは serving が読まない。週次 table と row を union しない |

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
- 最終週次 2026-09-18 残高は 2026-09-24 以後、clean かつ non-empty な snapshot を
  保存するまで再取得する。公表前の空 response を既存 cache が保持していても、最終週を
  欠損させないためである。
- 2026-09-28 以後、daily batch は stored trading days から「次の営業日が到来済み」の
  balance date だけを要求する。最初の対象は 2026-09-25 である。ただし calendar は移行実施の
  authority ではないため、`ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED` は初期値 `False` とする。
- U4 は JPX の移行実施告知と、repository が利用する J-Quants ClientV2 の endpoint / field /
  date identity / 母集団を実 payload で確認する。両方が一致した commit だけが activation flag を
  `True` にする。移行中止または ClientV2 未対応なら無効のままにする。
- 公式 go-live の確認後、activation 前の初回 snapshot は次の bounded probe で取得する。この flag は
  `2026-09-25..2026-09-25` 以外を拒否し、runtime activation は変更しない。取得が失敗した場合も
  flag は `False` のままである。

  ```bash
  uv run baibai-engine screening backfill-history \
    --start 2026-09-25 --end 2026-09-25 \
    --probe-margin-publication-transition
  ```

- U4 の初回 snapshot は、実データを見る前に固定した one-shot verifier で機械突合する。
  `row_count ratio 0.98..1.02`、小さい方の母集団に対する ticker overlap `>= 0.98`、
  `IssType` 一致率 `>= 0.95`、long / short 総残高比 `0.50..2.00` を shape / unit gate とする。
  6 残高は有限・非負かつ total = standard + negotiable を全 row で要求する。これらは別母集団、
  100 倍等の単位変更、field 入替を止めるための広い境界であり、日次需給軸の有効性判定ではない。
  実行例は次のとおりで、`pass` の report と actual payload 契約確認を同じ U4 commit に固定する。

  ```bash
  uv run python tools/diagnostics/verify_margin_publication_transition.py \
    --sqlite stores/market/market.sqlite \
    --output reports/operations/2026-09-28-margin-publication-transition/report.md
  ```

  exit `0` は全 gate pass、`1` は比較可能だが gate fail（fail report は保存する）、`2` は
  schema・coverage・公表時刻・row shape が比較不能、出力先が store と同一、または report の
  原子的保存に失敗した状態を表す。`1` / `2` では
  activation と merge を行わない。probe 取得、verifier `pass`、actual ClientV2 契約確認、
  activation flag 更新、full gates、merge の順序を変えない。
- 全銘柄日次の empty response は coverage 完了とみなさず、non-empty な clean snapshot を
  保存するまで次の batch で再取得する。
- J-Quants client/API が field、endpoint、日付 identity、母集団を変更した場合は取込を停止し、
  本表と fixture を更新してから再開する。既存 table への推測 mapping は行わない。
- 9/28 の移行中止告知が出た場合、全銘柄日次取込を開始しない。JPX の新しい実施日程を確認し、
  境界定数と fixture を同時に更新する。

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
持つ（週次は第 2 営業日、日次は翌営業日）。`ALL_ISSUES_DAILY_PUBLICATION_CONFIRMED` が `False`
の間は週次 balance date だけを返す。
