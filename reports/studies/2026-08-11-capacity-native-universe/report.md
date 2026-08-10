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

最低単元はas-of未調整終値×100株、p20は直近60市場sessionのnearest-rank 20%点とし、欠損・無出来を0円に含めた。100株は推測でなく、JPXが2018-10-01に全国取引所の内国株式について統一完了を公表しており、全cohortはその後に始まる（[JPX「売買単位の統一」](https://www.jpx.co.jp/equities/improvements/unit/index.html)）。

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
