---
title: "priced master 未評価銘柄の有界バイアス判定"
summary: "件数だけで cohort を落とす契約を、対象 row の同定・return 完全性・両側置換に対する結論方向の安定性で判定する契約へ改め、同条件の eligible を 3y 5→30、5y 4→12 に拡張した。"
doc_type: measurement-record
status: active
last_reviewed: 2026-08-01
---

# priced master 未評価銘柄の有界バイアス判定

価値tier: T2 — 3y/5y evidence の cohort 選択余地を縮め、後続する T1 の較正判断を少数 cohortへの依存から遠ざける。

## 1. 判定規則

規則・対象 metric・向き・採否条件は、計測前の commit `ef1bd2d` と [`2026-08-01-calibration-evidence-capacity-preregistration.md`](./2026-08-01-calibration-evidence-capacity-preregistration.md) §1 で固定した。

asof 当日に価格があり master にも在るが universe 評価へ入らなかった row を cache 上で同定する。報告値は観測した forward return を使い、感度計算だけで対象 row の return を全損 `-1.0` と置換前の resolved 流動性母集団中央値へ置換する。rank や E[r] は後付けしない。

次のすべてを満たす場合だけ件数 blocker を外す。

- diagnostics 件数と同定 row 数が一致する。
- 対象 row の forward return がすべて resolved である。
- `recommended_rank_top5`、`recommended_rank_top10`、`er_calibration` の報告値と両置換で、比較量が `> 0` か `<= 0` かが一致する。

## 2. 対象と freshness

production store を schema v4 で80 cohort再構築した。再構築結果は 1,509,220 forward rows、うち 1,055,260 resolved である。

既報との同条件比較は、2026-07-31 report が満期済みとして扱った 3y 44 cohort（〜2023-06）と5y 20 cohort（〜2021-06）に固定する。2026-08-01時点では3yの2023-07、5yの2021-07が新たに満期へ到達して各1件増えたが、どちらも対象 return 未解決で eligible には加わらない。

## 3. blocker の前後

blocker は cohort ごとの重複を許す。したがって列の合計は blocked cohort 数と一致しない。

| horizon | 満期済み | 変更前 eligible | 変更後 eligible | 新規 eligible |
| --- | ---: | ---: | ---: | ---: |
| 3y | 44 | 5 | **30** | **25** |
| 5y | 20 | 4 | **12** | **8** |

| blocker | 3y 前 | 3y 後 | 5y 前 | 5y 後 |
| --- | ---: | ---: | ---: | ---: |
| `priced_master_without_universe`（件数） | 38 | — | 16 | — |
| `priced_master_without_universe_return_unresolved` | — | 9 | — | 7 |
| `priced_master_without_universe_flips_direction` | — | 1 | — | 0 |
| `entry_price_gap` | 5 | 5 | 2 | 2 |
| `unpriced_exit_flips_direction` | 1 | 1 | 1 | 1 |

件数だけで落ちていた54 horizon-cohortのうち33件が新たに eligible になった。残る主因は「小数の対象 row があること」ではなく、その対象の forward return 自体が未解決であることへ絞られた。

### 新規 eligible cohort

3y（25件）:

`2019-12-30`, `2020-04-30`, `2020-07-31`, `2020-08-31`, `2020-09-30`, `2020-10-30`, `2020-11-30`, `2020-12-30`, `2021-02-26`, `2021-03-31`, `2021-04-30`, `2021-07-30`, `2021-09-30`, `2021-10-29`, `2021-11-30`, `2021-12-30`, `2022-03-31`, `2022-04-28`, `2022-07-29`, `2022-08-31`, `2022-10-31`, `2022-11-30`, `2022-12-30`, `2023-01-31`, `2023-02-28`

5y（8件）:

`2019-12-30`, `2020-02-28`, `2020-04-30`, `2020-07-31`, `2020-08-31`, `2020-11-30`, `2021-03-31`, `2021-04-30`

## 4. 不安定・未解決 cohort

実際に方向が割れたのは3y `2020-02-28` の1件だけである。

| metric | 報告値 | 全損 | 中立 |
| --- | ---: | ---: | ---: |
| `recommended_rank_top5` | 0.000000 | +0.000111 | 0.000000 |
| `recommended_rank_top10` | −0.019074 | −0.018963 | −0.019074 |
| `er_calibration` | +0.149100 | +0.149100 | +0.149100 |

`recommended_rank_top5` が `<= 0` から `> 0` へ動くため block する。この cohort は既存の `unpriced_exit` 感度でも block されるため、今回の判定が新たに eligible を減らしたわけではない。

対象 return が未解決の cohort は次である。括弧内は `同定数 / resolved 数`。

- 3y: `2020-03-31` (23/20), `2020-06-30` (5/4), `2021-06-30` (21/19), `2022-02-28` (8/7), `2022-06-30` (12/11), `2022-09-30` (9/8), `2023-03-31` (13/12), `2023-04-28` (8/7), `2023-06-30` (16/15)
- 5y: `2020-03-31` (23/19), `2020-06-30` (5/4), `2020-09-30` (7/6), `2020-10-30` (7/6), `2020-12-30` (26/25), `2021-02-26` (7/6), `2021-06-30` (21/18)

## 5. 判定

**規則を採用する。** 事前登録した条件を満たす。

- 合成 negative test で、方向 split、対象 return 欠損、diagnostics件数とrow同定数の不一致、未知 statusをそれぞれ fail closed にした。
- 置換は同定された対象 row の returnだけに適用し、rank・E[r]や他 rowを変えない。
- 同条件の eligible は3y 5→30、5y 4→12へ増え、cohortを事後選択できる余地を縮めた。
- `entry_price_gap` は変更せず、別 blockerのまま残した。

## 6. この結果が意味しないこと

この判定は未評価 row の valuation metricsやrankを復元せず、universe coverageが完全だったとも主張しない。両側置換は3 metricの方向の頑健性だけを答え、効果量の正確さ、統計的有意性、独立なtrack recordを示さない。eligibleが増えたこと自体はscreening methodの有効性を証明せず、後続の較正判断に使える観測を増やしただけである。
