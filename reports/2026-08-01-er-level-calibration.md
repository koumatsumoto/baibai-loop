---
title: "実現 FY 配当を使った E[r] 絶対水準較正"
summary: "FY 実績配当の近似 total-return 座標を構築し、11 cohort の 3y/5y production evidence で 0.10→0.14 候補を検証したが、horizon 方向不一致と誤差非改善により不採用とした。"
doc_type: measurement-record
status: active
last_reviewed: 2026-08-01
---

# 実現 FY 配当を使った E[r] 絶対水準較正

価値tier: T1 — E[r] の絶対年率を実現 total return と同じ座標で検証し、research・保有見直し・cash 判断に使う期待利回りの系統誤差を減らす。

## 1. 測定契約

集計規則、field、required scope、候補係数の推定式、採否条件は commit `e4dfe7e` と [`2026-08-01-er-level-calibration-preregistration.md`](./2026-08-01-er-level-calibration-preregistration.md) で結果計測前に固定した。

実現配当は `entry_date < fiscal_year_end <= exit_date` の FY 行から、同一 FY の最新 non-null `DivAnn` を一度だけ採る。各 DPS を forward store 最終 bar の株式基準へ adjustment factor で揃え、`price_return + ΣDPS / adjusted entry close` を total return とする。FY 行なし、DPS 欠損、factor 不完全は 0 円へ補完しない。端の FY は月割りしない。

`er_level_calibration` は current `er_annual` 昇順の quintile ごとに、予測 E[r] 絶対年率と実現 total return 絶対年率を比較する。既存 `price_return`、price-only metric、`er_calibration` は別 basis のまま維持する。

## 2. source と cache

| 項目 | 値 |
| --- | ---: |
| financial summary | 180,355 rows（2016-08-01〜2026-07-31） |
| FY `DivAnn` non-null | 38,758 rows / 4,527 tickers |
| calibration cache | schema v5 / 80 panels / 447MB |
| forward rows | 1,509,220 |
| price-only resolved | 1,055,260 |

schema v5 は forward row に `realized_dividend_sum`、`realized_dividend_fy_count`、`total_return`、`total_return_status`、`total_return_basis` を追加する。production cache は `--force` で全再構築し、price-only の row/resolved 件数は schema v4 の直前 store と一致した。

事前固定した 11 asof における coverage は次のとおり。件数は cohort 中央値で、`level n` は流動性母集団のうち E[r] 3成分と total return が揃う銘柄数、status は全 candidate forward row の内訳である。

| horizon | level n（min / median / max） | total resolved | DPS missing | FY observationなし | price unresolved |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1y diagnostic | 1,022 / 1,208 / 1,354 | 3,495 | 114 | 13 | 69 |
| 3y production | 956 / 1,121 / 1,251 | 3,231 | 236 | 6 | 213 |
| 5y production | 850 / 1,015 / 1,087 | 2,911 | 386 | 5 | 382 |

11 asof × 3y/5y の required 22 horizon-cohort は、core 3 metric と `er_level_calibration` がすべて eligible で、`production_change_allowed: true`、required scope 内の blocked/unresolved は 0 だった。欠損 row を外して required asof を選び直していない。

## 3. quintile 水準表

各値は required 11 cohort における cohort-level median の中央値。単位は年率、`n` だけ銘柄数である。

### 1y diagnostic

| Q | 予測 E[r] | 実現 total | error | 予測 reversion | 予測 carry | 実現 price | 実現 dividend差 | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | -5.00% | -6.11% | -1.11pt | -5.00% | 0.00% | -6.48% | +0.26% | 241 |
| 2 | -2.60% | +3.25% | +5.44pt | -3.70% | +1.13% | +1.86% | +1.32% | 242 |
| 3 | +0.16% | -2.67% | -3.17pt | -1.68% | +1.77% | -4.85% | +1.98% | 241 |
| 4 | +2.41% | +2.18% | -0.38pt | -0.08% | +2.54% | -0.81% | +2.63% | 242 |
| 5 | +5.57% | +13.08% | +7.51pt | +1.75% | +3.94% | +10.61% | +3.41% | 242 |

### 3y production evidence

| Q | 予測 E[r] | 実現 total | error | 予測 reversion | 予測 carry | 実現 price | 実現 dividend差 | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | -5.00% | -11.24% | -6.24pt | -5.00% | 0.00% | -11.79% | +0.40% | 224 |
| 2 | -2.55% | +0.62% | +3.08pt | -3.68% | +1.06% | -0.83% | +1.53% | 224 |
| 3 | +0.20% | +6.36% | +6.57pt | -1.61% | +1.80% | +4.07% | +2.13% | 224 |
| 4 | +2.54% | +10.82% | +8.80pt | +0.02% | +2.54% | +7.67% | +2.50% | 224 |
| 5 | +5.60% | +16.37% | +11.99pt | +1.75% | +3.98% | +13.33% | +2.94% | 225 |

### 5y production evidence

| Q | 予測 E[r] | 実現 total | error | 予測 reversion | 予測 carry | 実現 price | 実現 dividend差 | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | -5.00% | -8.72% | -3.58pt | -5.00% | -0.04% | -9.35% | +0.53% | 203 |
| 2 | -2.79% | +2.68% | +5.21pt | -3.87% | +1.02% | +0.95% | +1.62% | 203 |
| 3 | +0.20% | +7.93% | +7.68pt | -1.68% | +1.80% | +5.11% | +2.04% | 203 |
| 4 | +2.56% | +12.21% | +8.45pt | -0.02% | +2.54% | +9.44% | +2.40% | 203 |
| 5 | +5.62% | +15.66% | +10.94pt | +1.71% | +4.01% | +12.88% | +2.63% | 203 |

Q5 の実現 total return は予測を 1y +7.51pt、3y +11.99pt、5y +10.94pt 上回る。一方、Q1 は全 horizon で予測より悪く、中間 quintile の error も単調ではない。E[r] の順位情報と絶対水準の誤差は同じ問題ではなく、単一の reversion 係数を上げれば全 quintile の水準誤差が解ける形ではない。

## 4. `REALIZATION_RATE_ANNUAL` の判定

3y の quintile cell を使う事前登録済みの切片なし一回推定は `raw_rate_3y = 0.144512`、丸めた候補は `0.14` だった。確認側の 5y は `raw_rate_5y = 0.098720` で、現行 `0.10` を挟んで方向が一致しない。

| gate | 3y | 5y | 条件 | 判定 |
| --- | ---: | ---: | ---: | --- |
| raw rate の現行比方向 | 0.144512（上） | 0.098720（下） | 同符号 | **不通過** |
| median absolute error: current | 6.952% | 6.644% | — | — |
| median absolute error: candidate 0.14 | 6.905% | 7.162% | 両方で 1pt 以上改善 | **不通過** |
| error 改善幅 | +0.047pt | -0.518pt | 両方 +1pt 以上 | **不通過** |
| cohort 改善数 | 6 / 11 | 1 / 11 | 両方 8 / 11 以上 | **不通過** |

authority 成立以外の採用条件をすべて満たさない。したがって `REALIZATION_RATE_ANNUAL = 0.10` を維持し、0.14 の model variant、ranking replay、code change は行わない。#308 は「絶対水準座標で一回較正したが、3y/5y が同じ parameter change を支持しない」という non-adoption で閉じる。

## 5. 検算

合成 ticker `7203` fixture で次を固定した。

- entry 2,500円、exit 3,000円、窓内 FY DPS 50 + 訂正後60 + 70 = 180円。
- price return は `3,000 / 2,500 - 1 = 20.0%`、配当 return は `180 / 2,500 = 7.2%`、total return は 27.2%。
- 同一 FY の訂正前55円と訂正後60円は60円だけを採り、重複加算しない。
- exit 後の FY DPS は含めない。
- 1:2 split fixture は DPS 40→20、entry 100→50へ同じ basis で揃え、price 20% + dividend 40% = total 60% と一致した。
- FY 行なし、`DivAnn: null`、負値は unresolved、明示的な `DivAnn: 0` は total return を price return と同値で resolved にした。

focused calibration tests は 78 passed + 8 subtests。full gate は 2,172 passed + 376 subtests、Ruff、mypy（245 source files）、import contract 12件を通過した。authority と total-return cache に限定した独立レビューは、最新訂正の不正値 fallback と required metric status 欠落の2件を修正後に再確認し、残存 CRITICAL/HIGH/MEDIUM 0で PASS した。production/diagnostic evaluation artifact は `.cache/719-er-level-production.yaml` / `.cache/719-er-level-1y.yaml` で再現できる。

## 6. 限界

- 年間 DPS を FY 末へ帰属させる近似で、中間・期末配当の実際の権利落ち日や受渡日を再現しない。
- source に FY 行自体が無いケースを完全には識別できない。観測できた欠損は 0 円へ補完していない。
- 特別配当は `DivAnn` に含まれる範囲で入り、株主優待は入らない。
- realized dividend差は total-price の算術差で、予測 carry に含まれる buyback を直接観測しない。buyback の価格効果は price return と分離できない。
- required 11 asof は COVID 前後へ偏り、月次 forward 窓は重複する。有意性や独立な track record を主張しない。
- Q5 の正の error と Q1 の負の error は、単一係数の不足だけでなく cap、anchor、carry、forward 市場環境を含む。今回の結果から別 parameter や model 式を事後探索しない。
