---
title: "10 年履歴での較正 evidence 成立状況"
summary: "grid を 80 cohort へ広げ、3y で 44・5y で 20 の満期済み cohort が指標を算出できる状態にした。3y 5 件・5y 4 件が全 blocker を抜けている。"
doc_type: measurement-record
status: active
last_reviewed: 2026-07-31
---

# 10 年履歴での較正 evidence 成立状況

## 0. 結果

| | 拡張前 | 拡張後 |
| --- | ---: | ---: |
| grid の cohort 数 | 46 | **80** |
| forward rows / resolved | 876,220 / 476,386 | **1,509,220 / 1,037,427** |
| 3y 満期済み cohort | 0 | **44** |
| 5y 満期済み cohort | 0 | **20** |
| 指標を算出できた満期済み cohort | 0 | **3y 44 / 5y 20** |
| 全 blocker を抜けた cohort | 0 | **3y 5 / 5y 4** |

blocker を抜けた cohort: 3y が 2020-01-31 / 2020-05-29 / 2021-01-29 / 2021-05-31 / 2023-05-31、5y がそのうち先頭 4 件。

## 1. データ

| source | 範囲 | 件数 |
| --- | --- | ---: |
| 日次足 | 2016-08-01〜2026-07-29 | 10,096,935 |
| 財務サマリー | 2016-08-01〜2026-07-29 | 179,889 |
| 月末 master 断面 | 2016-09-30〜2026-07-29 | 128 日 |

grid は 2016-09〜2026-06 で 118 cohort を返すが、使えるのは **80**。日次足の入力窓は 1200 日で、2019-10-31 の窓（2016-07-18 起点）は取得下限 2016-08-01 を割る。**最初の非 clamp cohort は 2019-11-29** である。10 年分を取得しても cohort になるのは直近 6.7 年分にとどまる。

## 2. 解けた blocker

### 2.1 `input_range_clamped`（80/80）

10 年 backfill で解消。全 cohort が `bars_window_clamped: false`。

### 2.2 `master_snapshot` と `survivorship`（80/80）

`get_eq_master(date=)` は Standard で歴史断面を返す（2016-09-30 で 3,813 行）。月末グリッドを 2016-09 まで埋めた結果、全 80 cohort が `master_snapshot_status: exact_date` かつ `asof_population_mismatch_count: 0`（`survivorship_coverage_status: complete`）になった。

### 2.3 `market_out_of_scope`（再編前の全 cohort）

取引所は 2022 年 4 月に市場区分名を変えた。断面 master は各時点の名称を持つので、再編前の cohort は `ELIGIBLE_MARKETS` のどれにも一致せず、**master 全件が除外されていた**。

| asof | 区分の内訳 |
| --- | --- |
| 2019-11-29 | 東証一部 2,158 / JASDAQ スタンダード 673 / 東証二部 487 / その他 327 / マザーズ 302 / JASDAQ グロース 37 / TOKYO PRO MARKET 34 |
| 2022-09-30 | プライム 1,831 / スタンダード 1,450 / グロース 490 / その他 370 / TOKYO PRO MARKET 59 |

5y 満期済み cohort は例外なく再編前なので、この状態では 10 年分を取得しても 5y の評価対象が 1 件も無い。旧名称を範囲へ加えたところ、2019-11-29 cohort の除外は 4,018 → 361、universe は 3,656 になった。範囲の意図（個人が通常条件で買える国内主要市場、TOKYO PRO MARKET と その他 を除く）は再編前後で同じで、現在の master に旧名称は現れないため本番の universe は変わらない。

### 2.4 `unpriced_exit`

事前登録した両側代入へ置き換えた。満期済み 64 cohort すべてが廃止銘柄を含む（5y では母集団の 6〜12%）が、向きが割れた cohort は 0 件。詳細と限定は `2026-07-31-delisting-exclusion-preregistration.md`。

### 2.5 `adjustment_factor`

再構築で `adjustment_factor_coverage` が全 cohort で解決した。

## 3. 残る blocker

| blocker | 3y 満期済み 44 件中 | 5y 満期済み 20 件中 |
| --- | ---: | ---: |
| `priced_master_without_universe` | 38 | 16 |
| `entry_price_gap` | 5 | 2 |

`priced_master_without_universe` は「asof に値が付き master にも居るが panel が評価できなかった」銘柄で、cohort あたり 1〜31 件（流動性母集団 1,078〜1,494 に対して）。現行契約は 1 件でもあれば cohort を落とすので、これが残る cohort の主因である。件数が母集団に対して小さいため、廃止銘柄と同じ有界バイアスの扱いが適用できる可能性があるが、それは別の事前登録を要する。

## 4. この結果が意味しないこと

- **production 変更の authority を得たわけではない。** authority は `--required-asof` と `--required-metric` を事前に宣言した production_decision run が与える。ここで報告したのは「宣言できる cohort が存在するようになった」ことで、どの cohort を根拠にするかを結果を見てから選べば事前登録の意味を失う。
- **指標の中身は評価していない。** 本記録は eligibility の状態だけを述べる。E[r] や順位付けの精度に関する主張は含まない。
- **5y の 4 cohort は少数標本である。** forward 窓が重なる月次 cohort なので独立ではなく、cohort 数をそのまま標本数として読まない。

## 5. 検算

- grid の非 clamp 数: `month_end_asof_grid(2016-09-01, 2026-06-30)` が 118、うち 2019-11-29 以降が 80。
- 1200 日窓: 2019-11-29 − 1200 日 = 2016-08-16 > 取得下限 2016-08-01（非 clamp）、2019-10-31 − 1200 日 = 2016-07-18 < 下限（clamp）。
- 日次足の最大欠測間隔は 11 日（2019-04-26 → 2019-05-07、改元に伴う 10 連休）。他は 7 日以下。
