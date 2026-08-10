---
title: "E[r] 実現分布 context v2 と cap 2点比較"
summary: "現行E[r]の帯別下方分布を判断面へ正本化し、upside / buyback cap拡張は4窓すべてnegativeとして不採用にした。"
doc_type: measurement-record
status: complete
date: 2026-08-10
---

# E[r] 実現分布 context v2 と cap 2点比較

価値tier: T1 — E[r] の実現分布を判断面へ供給し、要求利回りの過剰棄却と value trap の見落としを減らす。

## 結論

context v2 は出荷する。現行 production authority が成立する証拠から、3y 34 cohort、5y 18 cohort の E[r] quintile と独立した `E[r] >= 8.5%` 帯を、median / q25 / q10 / trap率つきで正本化した。shortlist と research は method identity、45日expiry、schema全体が一致するときだけこの文脈を読む。

cap仮説の総合 verdict は、H-1 `UPSIDE_CAP 0.50 -> 1.00`、H-2 `BUYBACK_CLIP 0.05 -> 0.10` ともに `negative`。両仮説とも4窓すべてで事前登録した年率MAE 0.50pt改善を満たさない。H-1はtop-5の順位とtrapも大きく悪化する窓があり、H-2は順位が改善する窓を含むが水準誤差を改善しない。production定数、E[r]、FV、rank、gateは変更しない。

## authority とartifact

production decision の required scope は、3y / 5yの両方で core 3 metric と `er_level_calibration` がeligibleな共通15 cohort（2020-03-31〜2021-07-30）。`evidence_status: eligible`、`production_change_allowed: true`、blocking reason 0を確認した。その同じrunで、required scope外も含め個別integrityがeligibleな全cohortをhorizon別の主読みへ採った。

| artifact | SHA-256 |
| --- | --- |
| evaluation `.cache/891-er-context-v2-evaluation.yaml` | `c5d5a86b2cfc20d97e504d3640206f19ac41b6d88eb949788a5a56ae8ee4a851` |
| context `reports/published/er-level-calibration-latest.yaml` | `3bf0e90a02132bed9c24c1f538f3379b7e815430706d69612c91640042752607` |
| cap measurement `.cache/891-er-shape.yaml` | `3cdec86a21684de97ccccd109ac0a52f48471461c1facdd97be7d2986db418fe` |

事前情報の3y eligible 39件は現在のwrite-time integrityを再計算した値ではない。今回の実測は34件で、level metric自体を計算できたcohortのうち、`unpriced_exit_flips_direction` 5件、`entry_price_gap` 4件を除外した。結果を見てcohortを選び直していない。

## context v2 の実現分布

主basisの FY実績配当込み total return、ticker-as-of等重み。値は絶対年率、trapは同一cohortの母集団累積return中央値より20pt以上劣後した割合である。月次窓は重複し、独立標本または個別銘柄の予測を意味しない。

### 3y — 34 cohort / 2020-03-31〜2023-07-31

| band | E[r]表示境界 | predicted median | realized median | q25 | q10 | trap | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Q1 | `<= -3.626%` | -4.936% | -8.670% | -21.538% | -32.577% | 70.92% | 7,938 |
| Q2 | `> -3.626%, <= -0.752%` | -2.269% | +2.949% | -6.406% | -15.453% | 48.50% | 7,953 |
| Q3 | `> -0.752%, <= 1.511%` | +0.343% | +8.938% | +0.320% | -6.772% | 30.24% | 7,954 |
| Q4 | `> 1.511%, <= 3.602%` | +2.444% | +13.523% | +5.550% | -1.896% | 17.24% | 7,953 |
| Q5 | `> 3.602%` | +5.286% | +17.872% | +8.931% | +1.109% | 12.47% | 7,960 |
| E[r] >= 8.5% | `>= 8.500%` | +9.915% | +21.525% | +12.699% | +5.659% | 6.95% | 921 |

### 5y — 18 cohort / 2020-01-31〜2021-07-30

| band | E[r]表示境界 | predicted median | realized median | q25 | q10 | trap | n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Q1 | `<= -4.223%` | -5.000% | -6.441% | -16.562% | -26.643% | 79.45% | 3,650 |
| Q2 | `> -4.223%, <= -1.237%` | -2.513% | +3.770% | -3.048% | -9.712% | 54.39% | 3,657 |
| Q3 | `> -1.237%, <= 1.250%` | +0.262% | +8.820% | +2.228% | -3.518% | 35.93% | 3,657 |
| Q4 | `> 1.250%, <= 3.770%` | +2.555% | +12.077% | +5.946% | +0.164% | 21.44% | 3,657 |
| Q5 | `> 3.770%` | +6.329% | +16.921% | +9.624% | +3.076% | 13.20% | 3,666 |
| E[r] >= 8.5% | `>= 8.500%` | +10.102% | +18.040% | +11.175% | +5.362% | 10.10% | 713 |

共通15 cohortについても3y / 5yの同じ帯別分布をartifact内へ別保存した。これにより、horizon差と観測期間差を切り分けて再読できる。

## H-1 — upside cap

各MAEはcohort内absolute error中央値を作り、cohort等重みの中央値を比較する。top-N excessはtotal-return累積値、trapはticker等重み。単位`pt`はreturn差、改善率だけ比率である。

| window | verdict | MAE改善 | cohort改善率 | top-5 excess差 / trap差 | top-20 excess差 / trap差 |
| --- | --- | ---: | ---: | ---: | ---: |
| 3y design | negative | +0.009pt | 82.35% | -11.360pt / +1.176pt | +4.262pt / -0.882pt |
| 3y confirm | negative | +0.076pt | 64.71% | -29.497pt / +10.588pt | -11.011pt / +5.882pt |
| 5y design | negative | -0.022pt | 55.56% | +0.000pt / +6.667pt | +1.000pt / +1.667pt |
| 5y confirm | negative | +0.142pt | 66.67% | -53.689pt / +4.444pt | +11.195pt / +0.000pt |

全窓でMAE改善0.50ptを下回る。confirm側のtop-5は3y -29.50pt、5y -53.69ptと非劣後幅を大きく外し、trapも悪化した。capを広げて深いupsideを順位へ反映する仮説は採用しない。

## H-2 — buyback clip

| window | verdict | MAE改善 | cohort改善率 | top-5 excess差 / trap差 | top-20 excess差 / trap差 |
| --- | --- | ---: | ---: | ---: | ---: |
| 3y design | negative | +0.000pt | 47.06% | -5.584pt / -2.353pt | +4.292pt / -0.588pt |
| 3y confirm | negative | -0.011pt | 41.18% | +5.981pt / -21.176pt | +5.523pt / -2.353pt |
| 5y design | negative | -0.021pt | 44.44% | +0.000pt / +2.222pt | +0.000pt / +1.667pt |
| 5y confirm | negative | +0.000pt | 22.22% | +9.685pt / -6.667pt | -2.694pt / -0.556pt |

順位・trapが改善する窓はあるが、MAE改善は最大でも年率0.0004ptで、cohort改善率も全窓2/3未満。buyback clipを広げる水準根拠にならない。

## H-3 — 上下非対称診断

`abs(raw_upside) >= 5%`だけを使い、`annualized(price_return) / raw_upside`を集計した。採否には使わない。

| horizon / raw upside | n | realized rate median | q25 | q10 | 符号一致率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 3y negative | 27,907 | -0.101 | -0.681 | -1.593 | 42.34% |
| 3y nonnegative | 6,851 | +0.549 | +0.109 | -0.283 | 80.88% |
| 5y negative | 11,794 | -0.073 | -0.500 | -1.177 | 42.61% |
| 5y nonnegative | 4,381 | +0.473 | +0.153 | -0.120 | 85.69% |

negative raw upsideでは実現率中央値が負で符号一致率も約42%に留まる一方、nonnegative側は中央値が約0.47〜0.55、符号一致率81〜86%。Q1側の誤差を単純な対称cap拡張で直せない診断と整合するが、新しい式は事後探索しない。

## 独立検算とcleanup

context生成moduleとcap測定toolをimportせず、標準CSV readerから次を再計算した。

- 3y Q5: median `0.178724`、q10 `0.011094`、trap `0.124749`、n `7,960`。
- 5y Q5: median `0.169207`、q10 `0.030759`、trap `0.132024`、n `3,666`。
- H-1 / H-2の3y designについて、cohort MAE、改善率、top-5 / top-20 excess差、trap差がmeasurement artifactと完全一致した。

H-1 / H-2はnegativeのため、variant evaluator、専用test、`er_upside_raw` panel列を通常treeから削除する。再利用者が確認できない診断列をschemaへ残さない。context v2の生成・validator・判断面配線と、#893で共約したnullable空売り残高factだけをcache schema v14に残す。

## 制約

- FY実績配当はfiscal-year-end window近似で、実際の配当権利日を再現しない。
- 月次cohortのforward窓は重複し、統計的独立性や将来のtrack recordを主張しない。
- band境界はcohort-local quintile上端の中央値であり、現在の1銘柄を過去の固定母集団へ厳密に再分類するものではない。
- 8.5%帯の結果はhurdle変更のauthorityではない。hurdleは人間統治のまま維持する。
