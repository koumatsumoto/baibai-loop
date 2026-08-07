---
title: "pre-2019 375-session self-range panel 診断"
summary: "production と分離した診断 panel で2018年調整を含む20 cohortを構築した。2018-10〜12のE[r]上下差は1yで正、3yで2/3が正だが、深いstress一般やproduction parameterを支持する証拠ではない。"
doc_type: measurement-record
status: active
---

# pre-2019 375-session self-range panel 診断

価値tier: T2 — COVIDだけだった下落局面の観測に2018年調整を追加し、E[r]の局面頑健性について次の較正判断が使える診断範囲を広げる。

## 1. 入力契約と分離

規則・評価量・限定は、計測前の commit `ef1bd2d` と [`2026-08-01-calibration-evidence-capacity-preregistration.md`](./2026-08-01-calibration-evidence-capacity-preregistration.md) §2 で固定した。

| 項目 | 診断 variant | production |
| --- | --- | --- |
| store | `data/screening/calibration-pre2019/` | `data/screening/calibration/` |
| self-range | 375 sessions | 750 sessions |
| bar input | 600暦日 | 1,200暦日 |
| `rules_hash` | `b90289d71a0090b0` | `6a907ac39f6d45d3` |
| authority | diagnostic only | production契約に従う |

20 panels、362,395 forward rows（350,646 resolved）を local storeだけから構築した。72,459 panel rowsすべてが `self_range_degraded: true` で、`production_decision` を指定した実コマンドは `diagnostic panel variant has no production authority` と非0終了した。

### 事前見込みとの差

bar inputは20 cohortすべて非clampだったが、financial inputのcoverage床により `2018-03-30`〜`2018-07-31` の5件は統合 `input_range_clamped` が立った。事前登録はbar床だけから最初の非clampを概算しており、financial床を織り込めていなかった。

したがって完全な入力窓を持つ追加 cohort は `2018-08-31`〜`2019-10-31` の15件である。事前指定した2018年10〜12月の3件はすべて非clampで、1y/3yの `er_calibration` を算出できた。2016年は最初のpanelのbar input開始（2016-08-07）に含まれるが、2016年cohortが成立した意味ではない。

## 2. cohort別 E[r] 診断

predicted / realized は `er_reversion_annual` 最上位 quintile − 最下位 quintile の差、ratioは `realized / predicted` である。最初の5件は上記のinput clampを持つため参考値である。

| asof | 1y predicted | 1y realized | 1y ratio | 3y predicted | 3y realized | 3y ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2018-03-30† | +0.054 | +0.112 | +2.072 | +0.162 | +0.015 | +0.092 |
| 2018-04-27† | +0.052 | +0.037 | +0.714 | +0.155 | −0.036 | −0.233 |
| 2018-05-31† | +0.057 | +0.037 | +0.645 | +0.170 | −0.020 | −0.120 |
| 2018-06-29† | +0.059 | +0.039 | +0.656 | +0.175 | −0.027 | −0.152 |
| 2018-07-31† | +0.058 | −0.013 | −0.226 | +0.173 | −0.009 | −0.054 |
| 2018-08-31 | +0.061 | +0.003 | +0.043 | +0.181 | −0.047 | −0.261 |
| 2018-09-28 | +0.058 | +0.039 | +0.669 | +0.174 | −0.033 | −0.190 |
| **2018-10-31** | +0.067 | **+0.005** | +0.081 | +0.200 | **−0.030** | −0.148 |
| **2018-11-30** | +0.066 | **+0.042** | +0.637 | +0.198 | **+0.043** | +0.216 |
| **2018-12-28** | +0.080 | **+0.052** | +0.652 | +0.239 | **+0.085** | +0.357 |
| 2019-01-31 | +0.072 | −0.021 | −0.299 | +0.215 | +0.107 | +0.499 |
| 2019-02-28 | +0.069 | +0.015 | +0.216 | +0.205 | +0.105 | +0.513 |
| 2019-03-29 | +0.071 | +0.045 | +0.634 | +0.213 | +0.172 | +0.809 |
| 2019-04-26 | +0.069 | −0.075 | −1.085 | +0.206 | +0.107 | +0.518 |
| 2019-05-31 | +0.077 | −0.096 | −1.246 | +0.230 | +0.141 | +0.615 |
| 2019-06-28 | +0.071 | −0.163 | −2.282 | +0.214 | +0.071 | +0.330 |
| 2019-07-31 | +0.069 | −0.211 | −3.069 | +0.205 | −0.005 | −0.024 |
| 2019-08-30 | +0.074 | −0.139 | −1.874 | +0.223 | +0.163 | +0.731 |
| 2019-09-30 | +0.067 | −0.187 | −2.767 | +0.202 | +0.123 | +0.610 |
| 2019-10-31 | +0.060 | −0.228 | −3.797 | +0.180 | +0.068 | +0.376 |

† financial input clampを持つ参考値。

## 3. 2018年調整の読み

事前指定した2018-10〜12の中央値は、1y ratio `+0.637`、3y ratio `+0.216` だった。realized上下差は1yで3/3が正、3yで2/3が正である。少なくともこの3 cohortでは、調整局面に入った時点のE[r]順位が一律に逆転したとは読めない。

ただし、完全入力を持つ他のpre-2019 cohortとの比較でも結論は一方向でない。1yは2019年cohortの多くが負で、2018-10〜12の中央値の方が高い。一方3yは、2018-10〜12の中央値 `+0.216` が他 cohort中央値 `+0.330` より低い。

production 750-session panelのCOVID 2 cohortは、1y ratioが `+0.754` / `+0.662`、3yが `+1.230` / `+1.790` である。2018とCOVIDはinput contractもevent深度も違うので水準比較は診断に限られるが、特に3yでは2018 correctionの実現率がCOVIDよりかなり低い。第2の下落観測は加わったものの、「stressならE[r]が強い」または「弱い」という単一規則は支持しない。

## 4. 効果と複雑性の判定

**診断 variantを採用する。production parameterは変更しない。**

- 完全入力のpre-2019 cohortを15件、2018年調整の非clamp観測を3件追加できた。
- store、provenance hash、row quality、authority拒否の4層でproductionと分離した。
- surfaceは固定variant 1種類に限定し、任意窓の探索や永続parameterを追加していない。
- 結果は一方向のproduction判断を支持しないため、screening rules、E[r] parameter、production authority条件には反映しない。

## 5. この結果が意味しないこと

375-session self-rangeはproductionと異なる入力であり、3y/5y production evidenceではない。20 monthly cohortはforward窓が重なり、20個の独立標本ではない。2018年調整はCOVIDより浅く短いため、深いstress一般を識別しない。2018とCOVIDの差はevent深度、暦年、銘柄母集団、self-range窓が同時に違い、因果効果ではない。ratioの正負はE[r]順位の上下差だけを表し、投資track record、絶対return、統計的有意性を示さない。
