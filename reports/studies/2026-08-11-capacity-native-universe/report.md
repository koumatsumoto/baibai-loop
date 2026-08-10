# Capacity-native universe replay report

価値tier: T1 — 固定流動性floorの外側にある候補を実注文容量で回収しても、長期after-cost価値と退出可能性が維持されるかを判定した。

## 結論

**overall verdict: `negative`**

30万円・1%参加率・5日退出のcapacity contractは、outcome-freeな実行可能性を満たし、現行E[r]もcapacity-only集合内では1y/3y/5yすべて正方向だった。しかしpolicy置換としては、time holdout以降にtrapとstale exitが悪化し、3y/5yでは`capacity_core_top20`が`current_core_top20`の中央値非劣後floor（−2pt）を破った。capacity-only top5もcurrent boundary 21〜25を時間窓で安定して上回らない。

したがって固定 `market_cap >= 100億円 / average turnover >= 1億円 / listing span >= 182日` をcapacity contractへ置き換えない。production E[r]、FV、rank、gate、rules、selection payloadは変更しない。

## 誠実性境界とprovenance

順序は次で固定した。

1. outcome-free Stage 0をcommit `9a819392`で確定
2. effectを読む前に[`preregistration.md`](./preregistration.md)をcommit `09a1d5de`で確定
3. その後にだけforward replayを実行し、結果と独立検算をcommit `dce85051`へ固定
4. `negative` cleanupとして専用evaluator、test、study-local artifactを通常treeから削除

閾値、notional、参加率、退出日数、window、cost、basis、weighting、verdictは結果後に変更していない。重複する月次windowは独立標本ではなく、有意性・統計的優位・track recordを主張しない。

| artifact | SHA-256 | 所在 |
| --- | --- | --- |
| Stage 0 outcome-free artifact | `5c16ff8a180de0ea8be10ebda89897c0f7202a851262d3630f8b14bc46e59206` | commit `9a819392` |
| canonical calibration evaluation | `a9973c5e07a3230d35661754db146fd605ca141b0d928619666dbfef3b7ff9a0` | local rebuildable input |
| capacity replay result | `c42085241ee0d86496a20f4fff4377dc3d9288eb16ce21a3e925edf6afb6ffbe` | commit `dce85051` |
| independent verification | `9497f7c736ac22f9b54a83cfc72d853de80134cac25895caad8d8c7945b90b66` | commit `dce85051` |

## Stage 0 — outcome-free feasibility

入力は現行 `rules_hash: f4a3f3f20838e1cd` の80 cohort（2019-11-29〜2026-06-30）、screened candidate 123,978観測。market storeの最新日は2026-08-10だった。

最低単元はas-of未調整終値×100株、p20は直近60市場sessionのnearest-rank 20%点とし、欠損・無出来を0円に含めた。100株は推測でなく、JPXが2018-10-01に全国取引所の内国株式について統一完了を公表しており、全cohortはその後に始まる（[JPX「売買単位の統一」](https://www.jpx.co.jp/equities/improvements/unit/index.html)）。listing spanは上場日でなくas-of以前の最古daily barからのproxyで、入力窓の1200日で頭打ちになる。365日以上というmembership判定には使えるが、実際の上場年数としては解釈しない。

| sufficiency | 実測 | floor / ceiling | 判定 |
| --- | ---: | ---: | --- |
| capacity fact coverage | 98.65% | 95%以上 | pass |
| capacity top-20 availability | 100.0% | 90%以上 | pass |
| capacity-only outside-current top-5 availability | 97.5% | 75%以上 | pass |
| 1y design unique / max ticker share | 84 / 5.16% | 25以上 / 15%以下 | pass |
| 1y holdout unique / max ticker share | 39 / 12.17% | 25以上 / 15%以下 | pass |
| 3y all unique / max ticker share | 82 / 5.29% | 15以上 / 15%以下 | pass |
| 3y post-COVID unique / max ticker share | 48 / 7.50% | 15以上 / 15%以下 | pass |
| 5y matured unique / max ticker share | 44 / 10.68% | 8以上 / 15%以下 | pass |

primary capacity eligible数はcohort最小170、中央値786、最新886。capacity-onlyは延べ37,549観測・2,525銘柄で、時価総額中央値134億円、sector HHI 0.0924、carry優勢76.53%。「グロース市場かつ40〜100億円」は6.57%で、gateには使っていない。

capacity eligibleの最低単元中央値は111,500円、30万円を1%参加率で退出する日数中央値は0.73日、60-session zero-return share中央値は1.69%、no-trade share中央値は0%。current core top-20との重複中央値は50%で、比較集合は十分に異なった。

## Window verdict

全windowがcanonical `cohort_integrity`、policy availability、price/total resolution、unique ticker、concentrationのsufficiencyを満たした。

| window | matured / eligible | verdict | 主な判定 |
| --- | ---: | --- | --- |
| 1y design | 43 / 43 | `inconclusive` | incremental delta正share 51.16% < 55%、adverse resolution +2.36pt |
| 1y time holdout | 23 / 23 | `negative` | capacity trap +4.25〜4.57pt、incremental−boundary trap +7.09〜10.27pt |
| 3y all | 42 / 33 | `negative` | capacity median −3.81〜−6.14pt、trap +2.18〜2.72pt |
| 3y post-COVID | 24 / 19 | `negative` | capacity median −5.88〜−6.00pt、trap +2.09〜2.53pt |
| 5y all | 21 / 18 | `negative` | capacity median −10.09〜−16.88pt、trap +3.54〜7.32pt |

50bps / 200bps / 500bpsのどのbracketでも、price / total両basisのeffect gateは全windowで不通過だった。良いcost、basis、weightingだけを採用していない。

## 200bps主読み

値は同cohortの現行liquid population中央値に対する累積excess。`cap−current`はcore top-20同士、`incremental`はcapacity-only top5、`boundary`はcurrent rank 21〜25。

| window / basis | cap−current median | cap−current trap | incremental median | boundary median | cohort delta median / positive share |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1y design / price | +0.43pt | +0.20pt | +10.10% | +8.13% | +1.44pt / 51.16% |
| 1y design / total | +0.70pt | +0.38pt | +12.11% | +9.81% | +0.06pt / 51.16% |
| 1y holdout / price | +0.43pt | +4.57pt | +4.31% | +5.48% | −4.24pt / 43.48% |
| 1y holdout / total | +0.24pt | +4.25pt | +4.04% | +6.07% | −5.33pt / 43.48% |
| 3y all / price | −6.14pt | +2.18pt | +14.71% | +39.28% | −14.60pt / 36.36% |
| 3y all / total | −3.81pt | +2.72pt | +21.02% | +41.53% | −15.25pt / 30.30% |
| 3y post-COVID / price | −5.88pt | +2.09pt | +19.66% | +22.70% | +2.51pt / 52.63% |
| 3y post-COVID / total | −6.00pt | +2.53pt | +25.06% | +27.81% | −1.72pt / 42.11% |
| 5y / price | −16.88pt | +7.32pt | −4.83% | +56.21% | −66.76pt / 11.11% |
| 5y / total | −10.09pt | +3.54pt | +1.92% | +66.85% | −57.67pt / 22.22% |

## 全cost / basis / weighting感度

`ticker`は解決したticker-as-of等重み、`cohort`はcohortごとの集計を先に等重みした副weight。medianとtrapはそれぞれ `incremental / boundary`、`cap−current`だけは差分を示す。`delta / positive`は同一cohortのincremental median−boundary medianのwindow中央値と正shareで、costを両policyへ同額控除するためcost間で不変となる。

| window | cost | basis | ticker cap−current median / trap | ticker median | ticker trap | cohort median | cohort trap | delta / positive | effect |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1y design | 50 | price | +0.43pt / +0.07pt | 11.60% / 9.63% | 8.74% / 8.45% | 9.05% / 9.63% | 8.37% / 8.37% | +1.44pt / 51.16% | false |
| 1y design | 50 | total | +0.70pt / +0.63pt | 13.61% / 11.31% | 9.95% / 7.28% | 12.65% / 10.53% | 9.65% / 7.09% | +0.06pt / 51.16% | false |
| 1y design | 200 | price | +0.43pt / +0.20pt | 10.10% / 8.13% | 9.71% / 8.45% | 7.55% / 8.13% | 9.30% / 8.37% | +1.44pt / 51.16% | false |
| 1y design | 200 | total | +0.70pt / +0.38pt | 12.11% / 9.81% | 9.95% / 7.77% | 11.15% / 9.03% | 9.65% / 7.56% | +0.06pt / 51.16% | false |
| 1y design | 500 | price | +0.43pt / −0.27pt | 7.10% / 5.13% | 11.17% / 8.92% | 4.55% / 5.13% | 10.70% / 8.84% | +1.44pt / 51.16% | false |
| 1y design | 500 | total | +0.70pt / −0.12pt | 9.11% / 6.81% | 10.95% / 7.77% | 8.15% / 6.03% | 10.58% / 7.56% | +0.06pt / 51.16% | false |
| 1y holdout | 50 | price | +0.43pt / +4.33pt | 5.81% / 6.98% | 18.35% / 8.70% | 6.27% / 9.05% | 18.70% / 8.70% | −4.24pt / 43.48% | false |
| 1y holdout | 50 | total | +0.24pt / +4.22pt | 5.54% / 7.57% | 18.00% / 8.85% | 7.98% / 8.70% | 19.49% / 8.91% | −5.33pt / 43.48% | false |
| 1y holdout | 200 | price | +0.43pt / +4.57pt | 4.31% / 5.48% | 19.27% / 12.17% | 4.77% / 7.55% | 19.57% / 12.17% | −4.24pt / 43.48% | false |
| 1y holdout | 200 | total | +0.24pt / +4.25pt | 4.04% / 6.07% | 20.00% / 9.73% | 6.48% / 7.20% | 21.45% / 10.00% | −5.33pt / 43.48% | false |
| 1y holdout | 500 | price | +0.43pt / +4.40pt | 1.31% / 2.48% | 22.94% / 14.78% | 1.77% / 4.55% | 23.04% / 14.78% | −4.24pt / 43.48% | false |
| 1y holdout | 500 | total | +0.24pt / +4.11pt | 1.04% / 3.07% | 23.00% / 15.04% | 3.48% / 4.20% | 24.28% / 15.43% | −5.33pt / 43.48% | false |
| 3y all | 50 | price | −6.14pt / +2.17pt | 16.21% / 40.78% | 11.41% / 11.73% | 21.63% / 41.70% | 11.36% / 11.97% | −14.60pt / 36.36% | false |
| 3y all | 50 | total | −3.81pt / +2.39pt | 22.52% / 43.03% | 10.96% / 10.14% | 26.80% / 45.04% | 10.76% / 10.51% | −15.25pt / 30.30% | false |
| 3y all | 200 | price | −6.14pt / +2.18pt | 14.71% / 39.28% | 11.41% / 12.35% | 20.13% / 40.20% | 11.36% / 12.58% | −14.60pt / 36.36% | false |
| 3y all | 200 | total | −3.81pt / +2.72pt | 21.02% / 41.53% | 13.01% / 10.81% | 25.30% / 43.54% | 12.73% / 11.26% | −15.25pt / 30.30% | false |
| 3y all | 500 | price | −6.14pt / +2.36pt | 11.71% / 36.28% | 14.77% / 17.28% | 17.13% / 37.20% | 14.85% / 17.42% | −14.60pt / 36.36% | false |
| 3y all | 500 | total | −3.81pt / +2.37pt | 18.02% / 38.53% | 14.38% / 15.54% | 22.30% / 40.54% | 14.24% / 15.66% | −15.25pt / 30.30% | false |
| 3y post-COVID | 50 | price | −5.88pt / +2.07pt | 21.16% / 24.20% | 11.63% / 15.22% | 23.98% / 24.06% | 11.58% / 15.53% | +2.51pt / 52.63% | false |
| 3y post-COVID | 50 | total | −6.00pt / +1.96pt | 26.56% / 29.31% | 10.71% / 15.48% | 28.44% / 25.87% | 10.53% / 15.88% | −1.72pt / 42.11% | false |
| 3y post-COVID | 200 | price | −5.88pt / +2.09pt | 19.66% / 22.70% | 11.63% / 15.22% | 22.48% / 22.56% | 11.58% / 15.53% | +2.51pt / 52.63% | false |
| 3y post-COVID | 200 | total | −6.00pt / +2.53pt | 25.06% / 27.81% | 13.10% / 16.67% | 26.94% / 24.37% | 12.89% / 17.19% | −1.72pt / 42.11% | false |
| 3y post-COVID | 500 | price | −5.88pt / +1.83pt | 16.66% / 19.70% | 13.95% / 23.91% | 19.48% / 19.56% | 13.95% / 23.95% | +2.51pt / 52.63% | false |
| 3y post-COVID | 500 | total | −6.00pt / +1.28pt | 22.06% / 24.81% | 13.10% / 21.43% | 23.94% / 21.37% | 12.89% / 21.67% | −1.72pt / 42.11% | false |
| 5y | 50 | price | −16.88pt / +7.32pt | −3.33% / 57.71% | 35.90% / 9.09% | −8.13% / 62.18% | 34.81% / 8.89% | −66.76pt / 11.11% | false |
| 5y | 50 | total | −10.09pt / +3.22pt | 3.42% / 68.35% | 22.22% / 5.26% | 1.88% / 81.56% | 21.85% / 5.00% | −57.67pt / 22.22% | false |
| 5y | 200 | price | −16.88pt / +7.32pt | −4.83% / 56.21% | 35.90% / 9.09% | −9.63% / 60.68% | 34.81% / 8.89% | −66.76pt / 11.11% | false |
| 5y | 200 | total | −10.09pt / +3.54pt | 1.92% / 66.85% | 23.61% / 5.26% | 0.38% / 80.06% | 22.96% / 5.00% | −57.67pt / 22.22% | false |
| 5y | 500 | price | −16.88pt / +7.04pt | −7.83% / 53.21% | 37.18% / 9.09% | −12.63% / 57.68% | 35.93% / 8.89% | −66.76pt / 11.11% | false |
| 5y | 500 | total | −10.09pt / +4.50pt | −1.08% / 63.85% | 26.39% / 5.26% | −2.62% / 77.06% | 25.19% / 5.00% | −57.67pt / 22.22% | false |

## Capacity fact別の交絡診断

Stage 0のoutcome-free分布で固定した境界をcapacity-only top5へ適用し、200bps後のpopulation excessを分解した。各cellは `membership: price median/trap/n; total median/trap/n`。bucketは採否gateではなく、月次反復を含むため因果・有意性を主張しない。low bucketの小標本も比較根拠に使わない。

### Capacity days

境界はStage 0のp25 `0.2094日` とp75 `1.8388日`。

| bucket | 1y design | 1y holdout | 3y all | 3y post-COVID | 5y |
| --- | --- | --- | --- | --- | --- |
| low | 7: +29.83%/16.7%/6; +36.24%/20.0%/5 | 1: +106.66%/0.0%/1; +109.11%/0.0%/1 | 4: +40.78%/0.0%/4; +37.19%/0.0%/4 | 0: n=0; n=0 | 6: −43.36%/83.3%/6; −38.56%/80.0%/5 |
| middle | 160: +7.33%/12.2%/156; +8.75%/12.3%/154 | 74: +4.65%/25.0%/68; +6.07%/25.0%/68 | 130: +13.37%/11.7%/120; +20.04%/12.7%/118 | 78: +22.48%/9.9%/71; +28.77%/10.0%/70 | 63: −4.01%/30.5%/59; +1.92%/14.8%/54 |
| high | 46: +15.27%/0.0%/44; +20.15%/0.0%/42 | 40: −0.11%/10.0%/40; −2.19%/9.7%/31 | 30: +33.71%/12.0%/25; +33.11%/16.7%/24 | 17: +10.68%/20.0%/15; +5.94%/28.6%/14 | 19: +47.04%/38.5%/13; +67.41%/38.5%/13 |

### Zero-return share

境界は0%とStage 0のp75 `5.08%`。

| bucket | 1y design | 1y holdout | 3y all | 3y post-COVID | 5y |
| --- | --- | --- | --- | --- | --- |
| zero | 38: +5.47%/2.9%/34; +7.23%/3.0%/33 | 15: +12.79%/13.3%/15; +14.35%/13.3%/15 | 27: +26.46%/0.0%/25; +32.72%/0.0%/24 | 13: +19.81%/0.0%/11; +28.17%/0.0%/11 | 17: +49.88%/37.5%/16; +62.10%/35.7%/14 |
| >0–5.08% | 128: +10.05%/11.1%/126; +10.29%/11.5%/122 | 72: +0.54%/24.6%/69; −0.17%/26.7%/60 | 100: +15.72%/15.6%/90; +22.08%/17.0%/88 | 58: +23.72%/16.7%/54; +28.16%/17.3%/52 | 51: −3.57%/34.1%/44; +1.92%/21.4%/42 |
| >5.08% | 47: +14.01%/10.9%/46; +15.96%/10.9%/46 | 28: +8.55%/8.0%/25; +7.81%/8.0%/25 | 37: +7.42%/8.8%/34; +11.04%/11.8%/34 | 24: +13.81%/4.8%/21; +14.03%/9.5%/21 | 20: −10.96%/38.9%/18; −1.16%/18.8%/16 |

### Amihud illiquidity median

境界はStage 0のp25 `5.18e-11` とp75 `3.89e-10`。

| bucket | 1y design | 1y holdout | 3y all | 3y post-COVID | 5y |
| --- | --- | --- | --- | --- | --- |
| low | 6: +29.83%/16.7%/6; +36.24%/20.0%/5 | 0: n=0; n=0 | 4: +40.78%/0.0%/4; +37.19%/0.0%/4 | 0: n=0; n=0 | 6: −43.36%/83.3%/6; −38.56%/80.0%/5 |
| middle | 148: +5.35%/12.5%/144; +7.01%/12.7%/142 | 78: +4.87%/20.8%/72; +6.48%/21.4%/70 | 116: +14.26%/11.8%/110; +21.30%/11.9%/109 | 75: +22.48%/8.7%/69; +29.37%/8.7%/69 | 52: −6.69%/31.4%/51; +0.63%/13.0%/46 |
| high | 59: +23.02%/1.8%/56; +23.63%/1.9%/54 | 37: −0.60%/16.2%/37; −1.46%/16.7%/30 | 44: +13.87%/11.4%/35; +20.03%/18.2%/33 | 20: +10.68%/23.5%/17; +7.75%/33.3%/15 | 30: +41.91%/33.3%/21; +59.14%/33.3%/21 |

holdoutではcapacity daysとAmihudのhigh bucketがprice / totalとも中央値0以下へ落ち、5yではbucketを問わずtrapが高い。単一のilliquidity proxyだけで安全側を分離できる証拠にはならず、固定floor維持の結論と整合する。

## E[r] transportability と交絡

capacity-only全体では、E[r] Q5−Q1 median excessは全window・両basisで正だった。200bps後の範囲は1y holdoutの+9.23pt（price）が最小、5y totalの+100.71ptが最大。`E[r] >= 8.5%`帯のmedian excessも全windowで正（最小+1.34% price / 1y holdout）。Q5 trapはQ1より13.93〜45.96pt低い。

したがってnegativeの原因は「capacity-onlyでE[r]順位が完全に壊れる」ことではない。現行E[r]が同集合内で並べる力は残る一方、固定floor外の上位集合そのものがcurrent policy / boundaryよりtrap・退出不能・長期中央値で劣る。market-cap floorを外すproduction変更の便益を示せない。

## Unresolved / stale / delisting sensitivity

membershipを分母に残した。price unresolvedは全件 `unresolved_stale_exit` で、missing entry / missing exit / unknown statusは0件。stale exitにはauthoritativeなdelisting exit valueが無い銘柄を含むため、resolved rowへ推定値を入れていない。

| window | incremental membership / price / total resolved | boundary membership / price / total resolved | adverse rate delta |
| --- | ---: | ---: | ---: |
| 1y design | 213 / 206 / 201 | 215 / 213 / 206 | +2.36pt |
| 1y holdout | 115 / 109 / 100 | 115 / 115 / 113 | +5.22pt |
| 3y all | 164 / 149 / 146 | 165 / 162 / 148 | +7.33pt |
| 3y post-COVID | 95 / 86 / 84 | 95 / 92 / 84 | +6.32pt |
| 5y | 88 / 78 / 72 | 90 / 88 / 76 | +9.14pt |

total unresolvedはprice未解決に加え、FY配当欠損またはFY観測なしとして別statusに残した。全policy・全windowでprice resolved / membershipとtotal resolved / price resolvedは各75% floorを満たすため、欠損が結論を`insufficient`へ覆い隠していない。

## 独立検算

capacity evaluatorをimportしない標準CSV / SQLite readerで、次を再計算した。

| cohort | policy size | capacity-only quintile members | 結果 |
| --- | ---: | ---: | --- |
| 2023-06-30 / 1y | 20 / 20 / 5 / 5 | 508 | matched |
| 2022-06-30 / 3y | 20 / 20 / 5 / 5 | 614 | matched |
| 2020-06-30 / 5y | 20 / 20 / 5 / 5 | 573 | matched |

最低単元、nearest-rank p20、capacity days、4 policyのticker集合、population median、200bps後excess・trap、capacity-only E[r] quintile membershipが一致した。

## Cleanup と採否

`negative`契約に従い、capacity fact builder、replay evaluator、独立verifier、専用test、Stage 0 / result / verification artifactを通常treeから削除する。最終treeには事前登録と本reportだけを残し、計測時の完全artifactはSHA-256とcommit `dce85051`で固定する。

production接続issueは起票しない。同一仮説の再検定は、新規満期cohortまたはcapacity contract自体を変える新しい外部証拠がある場合に限る。
