# Candidate Discovery operational value baseline — 2026-09-01

## 結論

このstudyはCandidate Discoveryのalphaやmethod superiorityを証明しない。既存artifactから観測できるのは、16 cycle・320 judgmentが57 tickerへ集中し、Researchへ進めたのは18件（5.6%）、既観測tickerの再判断263件のうち直前も今回もskipだったものが240件（91.3%）という運用実態である。latest-mainのv2 rollout cycleでは、Review Setとpure E[r] top20のoverlapは0/20、Review Set E[r]中央値は1.685%、負値は7/20だった。これはCandidate Discoveryが高E[r]順ではないという既存境界を測った結果であり、その良否を確定するものではない。

T1（良い候補供給）は未確定、T2（高値・permanent loss・data defectを止めたか）はjudgment yieldとskipまでは観測可能だがoutcome未接続、T3（人間時間・運用負荷）は低いResearch yieldと高いrepeat-skip、および今回の入力削減から改善余地と改善内容を観測できる。method tuningの開始条件であるv2 6 cycle / 60 unique tickerは、rollout後も1 / 20であり未達である。

## 対象と再現条件

- 観測日: 2026-09-01 JST
- application DB: schema 19、SHA-256 `c7d2582b9713670d87f67b5eb2fffdf944d77dc27914490b67a964f5aaa64c28`
- run store: schema 5、SHA-256 `ffc50b4dfc8a3a284f7b9caece1577b079b8ff317fa440689b5757f7c8e15a9d`
- 両storeとも`integrity_check=ok`、`foreign_key_check`違反0
- application DBの全Research Triage 16件を対象とした。historical v1 15件は変更せず、latest-main rolloutでv2を1件追加した
- approach/support/E[r] opportunity-costのfull-fidelity集計は、nominationsとsource Security Analysisが残るbaseline 2 cycleとlatest-main v2 rollout 1 cycleだけを対象とした。run prune済みの13 cycleを推測で補完しない
- 集計はread-only SQLite/JSON projectionで一時実行し、新table、event、job、dashboard、stable CLI、集計scriptを残していない

## Candidate supply

| 指標 | 観測値 | 読み方 |
| --- | ---: | --- |
| published Triage cycle | 16 | v1 history 15件 + v2 rollout 1件 |
| judged entries | 320 | 各cycle 20件 |
| unique ticker | 57 | 263件は再登場観測 |
| 隣接cycle再登場率 | 平均84.0% | 15 transition、最小0/20、最大20/20 |
| research / skip | 18 / 302 | research yield 5.6% |
| repeat-skip | 240 / 263 = 91.3% | tickerの前回観測も今回もskip。自由文理由の自動分類はしていない |
| 全320件 E[r]中央値 | 8.450% | 異なるhistorical methodを混ぜた記述統計で、performance比較には使わない |
| 全320件 E[r]負値 | 23 | baseline current 2 cycleで16、v2 rolloutで7 |

### cycle別human judgment

| as-of / Triage | research | skip | yield | Review Set E[r] median | E[r]負値 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-07-31 yen-floor-earnings-week | 6 | 14 | 30% | 8.435% | 0 |
| 2026-08-03 earnings-week-front-load | 3 | 17 | 15% | 8.560% | 0 |
| 2026-08-04 core-below-hurdle | 0 | 20 | 0% | 8.620% | 0 |
| 2026-08-06 carry-only-hurdle | 0 | 20 | 0% | 8.905% | 0 |
| 2026-08-07 screening-e2e-validated | 0 | 20 | 0% | 9.010% | 0 |
| 2026-08-10 daily-reviewed | 0 | 20 | 0% | 8.895% | 0 |
| 2026-08-10 carry-window-expiry | 1 | 19 | 5% | 8.795% | 0 |
| 2026-08-13 basis-corrected-carry-review | 1 | 19 | 5% | 8.660% | 0 |
| 2026-08-14 carry-frames-closed | 1 | 19 | 5% | 8.555% | 0 |
| 2026-08-19 fv-gap-reresearch | 1 | 19 | 5% | 8.850% | 0 |
| 2026-08-21 boundary-band-trigger-check | 0 | 20 | 0% | 8.615% | 0 |
| 2026-08-26 carry-quality-review | 1 | 19 | 5% | 8.495% | 0 |
| 2026-08-26 gate-writing-review | 1 | 19 | 5% | 8.495% | 0 |
| 2026-08-28 operational-validation-corrected | 1 | 19 | 5% | 1.365% | 8 |
| 2026-08-28 current-method-operational-validation | 1 | 19 | 5% | 1.365% | 8 |
| 2026-08-28 v2-rollout-validation | 1 | 19 | 5% | 1.685% | 7 |

## current 4-approach contractの断面

full-fidelity 2 cycleは同じ20 ticker・同じcompositionなので、以下は「1 cycleあたり / 2 cycle合計」で示す。

| 指標 | 1 cycle | 2 cycle合計 |
| --- | ---: | ---: |
| single-support | 14 | 28 |
| multi-support | 6 | 12 |
| support=2 | 5 | 10 |
| support=3 | 1 | 2 |
| target充足phaseのslot | 13 | 26 |
| target充足後のcapacity fill | 7 | 14 |

approachごとのReview Set contributionとjudgment:

| approach | supported entries | unique contribution | research | skip |
| --- | ---: | ---: | ---: | ---: |
| Current Earnings Power | 8 / 16 | 4 / 8 | 0 | 16 |
| Normalized Earnings Power | 8 / 16 | 3 / 6 | 2 | 14 |
| Asset Value | 5 / 10 | 4 / 8 | 2 | 8 |
| Reinvestment Value | 6 / 12 | 3 / 6 | 0 | 12 |

pair overlapは1 cycleあたり、Current+Normalized 3、Current+Reinvestment 2、Normalized+Reinvestment 2、Asset+Normalized 1。3-support entryは各pairへ1件ずつ含めた。support-count別ではsingle-support 28件がすべてskip、support=2はresearch 2 / skip 8、support=3はskip 2だった。標本は2 duplicate cycleだけなので、approach採否やtarget変更の根拠にはしない。

pure E[r] top20はsource Security Analysis 3,705件を`er_annual DESC, ticker`で並べたdiagnosticで、両cycleともReview Set overlap 0/20、pure E[r] top20下限は8.55%だった。Review Set membership/orderへE[r]を使わない境界をそのまま測ったものであり、E[r]側を正解ラベルとは扱わない。

latest-main v2 rolloutはbaseline 2 cycleとは別のlive runであり、20件中16件が直前cycleから再登場した。support構成はsingle 13、support=2が6、support=3が1。approach別のsupported / unique contribution / research / skipは、Current Earnings Power `7 / 2 / 0 / 7`、Normalized Earnings Power `9 / 4 / 0 / 9`、Asset Value `6 / 4 / 0 / 6`、Reinvestment Value `6 / 3 / 1 / 5`だった。pure E[r] top20とのoverlapは0/20、下限は8.55%である。live inputの変化によるcycle差を、cleanupによるselection変更とは扱っていない。同一frozen runに対する回帰testではmembership / order / nominations / analysisが不変である。

## Downstream explicit linkage

- `capital_allocation_assessment.research_triage_id`という明示FKで接続できたのは2 Triage / 2 assessmentで、resultはいずれも`no_allocation`
- assessmentの`alternatives[].thesis_id`という明示referenceを介して2 Triage / 2 thesisへ接続できた。2 assessmentとも`no_allocation`なので、この明示chain上で新規allocationは0件
- portfolio outcomeへの明示foreign/referenceはないため、Triage起点の3y/5y outcomeは`unavailable`
- tickerと日付の近さでは補完していない。したがって`unavailable`を0件と解釈しない

この断面から「買付0だからscreening失敗」「skipが多いから成功」「多様性があるから低E[r]でも問題なし」のいずれも結論できない。T1/T2には3y/5yのmethod-faithful cohortと明示downstream linkageが必要である。

## Simplicity before / after

| seam | before | after | operational effect |
| --- | --- | --- | --- |
| Review Set publish | required 2 args、run store + application DB state | required 2 args、run storeのみ | L2 compositionからcross-store dependencyを1つ削除 |
| Triage scaffold | required 2 inputs（Review Set file path、output path） | required 2 inputs（Review Set ID、output path） | 人間がsource file pathを持ち回らない |
| Research prepare | required 4 inputs（as-of、Review Set file path、Triage ID、workspace） | required 2 inputs（Triage ID、workspace） | 重複入力2つ、external source file path 1つを削除 |
| publish後のResearch再開 | application DB + copied Review Set file + file SHA | application DBのv2 Triageのみ | source Review Set/run store prune後もprepare/status可能 |

workspaceはauthorityではなく、canonical Triage ID/hash/researchable ticker集合とledger append headの照合対象である。これは新しいaudit storeではなく、skip ticker混入とstale local copyをその場で止めるT1/T2 gateである。

## T1 / T2 / T3の読み分け

- **T1 candidate quality**: v2 rollout Review Setがpure E[r] top20と0件overlapし、中央値1.685%だった事実はopportunity costを示す。しかし1 v2 cycleと未成熟outcomeでは、どちらが良い候補集合かは未確定
- **T2 stop quality**: 302/320 skipとv2 cycleの7/20 negative E[r]は人間gateが多くを止めた事実。永久損失・高値・data defectを正しく止めたかは自由文分類や推測をせず、3y/5y outcomeまで未確定
- **T3 operating value**: research yield 5.6%、repeat-skip 91.3%は人間時間の重複可能性を示す。今回のcutoverはResearch prepare required inputsを4→2、source file pathを1→0、publish後cross-store dependencyを2→1へ減らす

## 次の判断条件

performance tuningはv2 Research Triage 6 cycle以上かつv2 judged unique ticker 60件以上になるまで開始しない。production tuningはさらにcurrent methodを忠実に再現する3y/5y cohortを要求する。rollout後は同じ既存artifact projectionを更新し、novelty単独や1 cycleの結果をsuccess判定にしない。

## Rollout validation

PR B/C merge後のlatest main `af24d708126e1481528bee2356124e7668a319f4`で実運用を一巡した。

- screening run: `run-revision-174755085cd4454bb859428fc222c1cb`、Security Analysis 3,705件
- pure Review Set: `review-set-20260828-54b964d7791d`。`review_capacity=20`、`nomination_depth=20`、representation targets `6 / 5 / 5 / 4`を満たし、unfilled targetは0
- Review Set order: `6547, 7280, 7279, 7888, 9130, 6419, 4996, 2501, 5019, 6986, 5946, 9401, 4222, 2168, 5021, 9501, 3660, 5210, 4628, 5351`
- Research Triage v2: `research-triage-20260828-v2-rollout-validation`。20件のjudgment-time snapshotを持ち、research 1件（5021）/ skip 19件
- `research prepare --research-triage-id`だけでworkspaceを作成し、manifestへcanonical Triage payload SHA-256 `19350b31bc42d46060aaf517b8f05821ef581aaee7ac92b45039ca251865d357`、researchable ticker `5021`、ledger append head `25`を記録した
- source Review Set fileをResearch入力に使わず、workspace statusは`awaiting_research_set_admission`まで到達した。5021のResearch開始は人間確認が必要なため、operationを`awaiting-human-research-set-confirmation`でcheckpointし、架空の投資判断を作っていない
- application DBはschema 19、`integrity_check=ok`、foreign key違反0。historical v1 rowは削除・書換えしていない

このrolloutは「同じCandidate Discoveryを少ない入力で再開できる」T3改善と、skip ticker混入をcanonical v2 snapshotで止めるT1/T2境界を確認した。5021が良い投資であることやalpha改善は確認していない。
