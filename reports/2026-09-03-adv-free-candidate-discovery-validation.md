# ADV-free Candidate Discovery validation

価値tier: T1 — 低流動性という非価値signalだけで候補をAI比較前に落とさず、4 Approachの価値仮説をResearch Triageへ供給する。

## Scope

- before rules: `multi-valuation-v3` (`2026-09-03T142050+0900`)
- after rules: `multi-valuation-v4` (`2026-09-03T161939+0900`)
- market store: 同一のproduction-equivalent local copy
- as-of: `2026-09-01`
- before Review Set: `review-set-20260901-ddf5b236e6c9`
- after screening run: `run-revision-9962414f2ee648b6b5ce374544a98536`
- after Review Set: `review-set-20260901-5ce061e6df6e`
- after Research Triage: `research-triage-20260901-46b4f2c3f6dedd57`

検証はcanonical storeから分離したcopyで行った。screeningは3,706銘柄を分析し、既存のTTM exactness / earnings-calendar coverageについて`partial warning`を返したが、Candidate Discovery input gateは通過した。

## Population and Nomination delta

共通eligibilityは1,583から2,398へ増加した。差分815銘柄はすべて既存条件（時価総額100億円、上場期間182日、JPX規制状態）を満たし、ADVだけが1億円未満だった銘柄である。このsnapshotにはADV欠損銘柄はなかった。欠損ADVもeligibilityから外さないことはnegative testで固定した。

| Approach | before native eligible | after native eligible | top20 added / removed |
| --- | ---: | ---: | ---: |
| Current Earnings Power | 1,517 | 2,308 | 10 / 10 |
| Normalized Earnings Power | 1,231 | 1,877 | 11 / 11 |
| Asset Value | 665 | 1,084 | 16 / 16 |
| Reinvestment Value | 60 | 104 | 12 / 12 |

各Approachは20 nominations、合計80 membershipsを維持した。exact Nomination unionは71から75へ変化し、45銘柄が追加、41銘柄が退出した。退出は別gateによる除外ではなく、ADV-free comparison populationでApproach内rankとsector / market medianが再計算された結果である。

追加候補の例:

| ticker | ADV 億円 | market cap 億円 | Approach / rank | native coordinate | machine E[r] | Triage |
| --- | ---: | ---: | --- | --- | ---: | --- |
| 3675 | 0.2 | 115 | Reinvestment / 1 | P/S sector gap -80.74% | 3.15% | research #1 |
| 7463 | 0.3 | 322 | Current / 3, Normalized / 5 | trailing PER 2.79x, normalized PER 4.21x | 8.41% | research #2 |
| 3020 | 0.1 | 119 | Current / 17, Normalized / 20 | forward PER 5.52x, normalized PER 6.78x | -1.26% | research #7 |
| 4231 | 0.2 | 205 | Asset / 15 | asset-backed ratio 1.057x | 1.82% | research #11 |
| 2112 | 0.3 | 132 | Normalized / 16 | normalized PER 6.17x | 0.03% | research #14 |
| 2415 | 0.1 | 176 | Asset / 14 | asset-backed ratio 1.132x | 3.45% | research #15 |
| 5363 | 0.3 | 311 | Asset / 20 | asset-backed ratio 1.017x | 1.25% | research #18 |
| 2221 | 0.1 | 314 | Asset / 1 | asset-backed ratio 2.149x | -0.80% | research #19 |
| 3954 | 0.0 | 142 | Asset / 11 | asset-backed ratio 1.171x | -2.56% | skip |
| 5918 | 0.0 | 165 | Asset / 2 | asset-backed ratio 1.995x | 0.37% | skip |

## AI Research Triage

AIは75候補を1つのread-only / no-tool requestで比較した。成功runは155.63秒、input 65,784 tokens、output 8,375 tokens、tool call 0だった。strict validation済みのpublicationは75/75 decisions、research 23、skip 52、research priority 1..23である。旧union外の追加45候補では16件がresearch、29件がskipとなった。

低ADVはresearchを妨げていない。ADV 0.2億円の3675がpriority 1、0.3億円の7463がpriority 2であり、負のE[r]を持つ3020と2221もApproach仮説からresearchへ進んだ。ADV / 流動性へ言及した7件のうち5363はresearch、skip 6件はいずれもFCF、収益性、valuation、価値実現経路の反証を併記した。固定ADV値だけを理由にしたskipはなかった。

実AIの初回出力ではskipに禁止fieldが入り、次の出力ではpriorityに欠番・重複があった。どちらもstrict validatorがwrite前に拒否した。policyを、skipの禁止fieldはJSON null、research priorityはresearch件数Nに対してexactly 1..Nで重複・欠番なしと明示する形へ修正し、再実行でpublicationまで通した。validatorの緩和やpriorityの後付け補正は行っていない。

検証開始時からactiveなResearch Operationが1件存在したが、Review SetとTriageは生成できた。Triage publish後もactive Operationは同じ1件で、TriageによるOperation自動開始はなかった。

## Calibration

ADV-free current rulesでproduction-authority calibrationを全期間再構築した。current snapshotは81 cohorts、305,418 panel rows、1,527,495 forward rowsで、全81 cohortsがforward-ready。calibration method hashは`37aa2c1014642287`、serving用screening rules hashは`c525a6c55309450f`である。

E[r] contextはestimator-policy用の3y / 5y evidenceとして、両horizonでintegrityと`er_calibration` / `er_level_calibration`がeligibleな18 as-ofを明示scopeに再発行した。common windowは`2019-12-30`から`2021-07-30`。ADV-free母集団では`2020-06-30`がentry price gap、`2021-04-30`が5y unpriced-exit direction sensitivityでblockされたため除外した。評価器の緩和や、blockされたcohortの採用は行っていない。
