---
title: "3 FY正規化PERのproduction判断"
summary: "raw annotationは採用候補、sector FV anchorは不採用とする長期authority評価。"
doc_type: report
status: active
date: 2026-08-02
---

# 3 FY正規化PERのproduction判断

価値tier: T1 — 当期利益で割安に見えるtrapを一次research前に識別し、誤ったFV/E[r]分母へ接続せず候補判断を改善する。

## 結論

- **H-W raw annotation: `adoption_candidate`**。`normalized_per_3fy`を`per_trailing`と並べる参考値に限定し、warningの真偽、gate、rank、FV、E[r]には接続しない。
- **H-A normalized sector FV anchor: `negative`**。5yの増分効果が負で、3y / 5yともcohort勝率基準を外した。専用evaluationは通常treeから除去する。

production表示は本issueに含めない。H-Wは既存candidate metric / shortlist比較表への最小配線を別issueで検討する。H-Aは第三のFV anchor、既存FV、E[r]の変更へ進めない。

## 固定scopeとauthority

事前登録 `820bb7d`、結果を読む前に固定した評価実装 `f5bbc0d` を用いた。required as-ofは次の4点、horizonは各`3y` / `5y`の8組である。

- `2020-01-31`
- `2020-05-29`
- `2021-01-29`
- `2021-05-31`

H-Wだけをrequired metricにしたproduction authorityは8/8 eligibleだった。`normalized_per_3fy`のdelisting / priced-master欠損に対する全損・中立代入の方向も8/8で安定した。

H-Aを同時にrequiredとした一次判定は6/8 eligibleだった。`2021-01-29`の3yと5yで、normalized sector anchorの採用条件がdelisting代入により反転したためである。これはH-Aの不採用根拠へ加える。事前登録でH-WとH-Aを別surfaceとして定義しているため、H-WのauthorityはH-Aをrequired metricから外した同一cohort・同一horizonで確認した。

## H-W: raw annotation

| horizon | mean decile spread | positive cohort share | mean coverage |
| --- | ---: | ---: | ---: |
| 3y | +73.70pt | 100% | 73.14% |
| 5y | +142.72pt | 100% | 73.18% |

全controlでmean stratified median spreadは正、mean trap deltaは0以下だった。

| control | 3y median spread | 3y trap delta | 5y median spread | 5y trap delta |
| --- | ---: | ---: | ---: | ---: |
| `per_trailing` | +17.54pt | -13.69pt | +34.42pt | -14.31pt |
| `pbr` | +15.09pt | -13.11pt | +27.05pt | -13.70pt |
| `market_cap_oku` | +26.03pt | -23.84pt | +60.00pt | -31.97pt |
| `sector_33` | +19.73pt | -16.45pt | +42.76pt | -22.95pt |

required 8組のauthority、両horizonのspreadと勝率、全control、60% coverageをすべて満たす。表示時も値の意味は「現在株価 ÷ 3 FY平均EPS」というestimateに限定する。正常利益、cycle調整済みFV、安全性の判定とは呼ばない。

## H-A: normalized sector FV anchor

| horizon | normalized−current mean median | positive cohort share | mean trap delta | normalized / current mean spread | changed share | common n |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 3y | +3.24pt | 25% | -1.02pt | +46.20pt / +33.19pt | 100% | 3,261 |
| 5y | -5.65pt | 50% | -0.44pt | +79.31pt / +66.55pt | 100% | 3,122 |

3yは平均deltaだけが`>= +3pt`を満たしたが、正だったのは1/4 cohortだった。5yは平均deltaが負で、正だったのも2/4 cohortにとどまった。順位変更、group size、trap非悪化、normalized axis spreadは条件を満たすが、増分価値の方向がcohort間で再現しない。

特に`2020-05-29`の5yはmedian delta -24.73pt、trap delta +2.41ptだった。良いaxis単体をsector FVへ変換しても、現行trailing sector anchorより良いtop decileを一貫して作るとは限らない。したがって第三の参考FVを増やす複雑性に見合わない。

## 独立検算

`2020-01-31` 3yをevaluation関数から独立してpanel / forward CSVから再計算した。`in_population`の正の倍率からsector medianを作り、sector n < 10を市場medianへfallbackし、resolved共通782銘柄の上位decileを再選定した。

- current / normalized group n: 79 / 79
- overlap n: 37
- current / normalized median excess: +7.5052% / +5.9816%
- median delta: -1.5236pt
- current / normalized trap rate: 21.52% / 22.78%
- trap delta: +1.26pt

評価出力と一致した。固定4 as-of以外を含む全cache aggregateは採否に使用していない。forward窓が重複するため、4 cohortを独立標本や有意性として扱わない。

## 再現

```bash
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 3y --horizon 5y \
  --run-purpose production_decision \
  --required-asof 2020-01-31 --required-asof 2020-05-29 \
  --required-asof 2021-01-29 --required-asof 2021-05-31 \
  --required-metric recommended_rank_top5 \
  --required-metric recommended_rank_top10 \
  --required-metric er_calibration \
  --required-metric normalized_per_3fy \
  --out /tmp/normalized-per-warning-eval.yaml
```

H-Aの再現実装はcommit `f5bbc0d`に固定されている。通常treeには、採用候補となったH-Wのauthorityに必要な`normalized_per_3fy` metric statusと感応度だけを残す。
