# Candidate Discovery operational value baseline — 2026-09-01

## 結論

このstudyはCandidate Discoveryのalphaやmethod superiorityを証明しない。application DBには16件・320 judgmentのimmutableなpublished Triage revisionsがあるが、これらを16 independent operation cyclesとは数えない。current contractで明示接続できるv2 operation cycleは1件で、human Research Set確定待ちのcheckpointにある。したがって、全revisionを使ったResearch yieldやrepeat-skipはpublication履歴の記述統計であり、独立した運用performance標本ではない。

T1（良い候補供給）は未確定、T2（高値・permanent loss・data defectを止めたか）はpublication上のjudgmentとskipまでは観測可能だがoutcome未接続、T3（人間時間・運用負荷）は今回の入力削減を観測できる。method tuningの開始条件であるexplicit linked v2 operation cycle 6件 / v2 judged unique ticker 60件は、rollout後も1 / 20であり未達である。

## 2026-09-01 correction — comparison universeとcycle分母

初版には2つの測定誤りがあった。過去値は削除せず、以下を正本へ訂正する。

| 指標 | 旧値 | 補正後値 | 差が出た理由 |
| --- | ---: | ---: | --- |
| pure E[r]母集団 | all Security Analysis 3,705件（finite 3,635件） | production eligibilityを満たすcommon eligible population 1,592件（finite E[r] 1,591件） | 旧値はReview Setが選択できない銘柄をchallengerへ含めていた |
| pure E[r] top20 lower bound | 8.55% | 7.45% | 同じrunにmatchingするrules identityでeligibilityを再適用した |
| pure E[r] top20 range / median | 8.55–10.92% / 8.96% | 7.45–9.10% / 8.46% | comparison universeを揃えたため |
| pure E[r] top20 negative count | 0 | 0 | 両母集団ともtop20には負値なし |
| Review Set overlap | 0/20 | current-method v1: 0/20、v2 rollout: 1/20 | v2 Review Setでは5021がsame-universe challengerにも入る |
| operation cycle分母 | 16 | explicit linked v2 operation cycle 1 | 16はDB上のpublished revisionsであり、v2 Triage・distinct Review Set・human checkpoint/completionの接続を要求していなかった |

all Security Analysis版は`all Security Analysis diagnostic`としてのみ残す。feasible challenger、opportunity cost、Candidate Discovery performance comparisonには使わない。補正後のpure E[r]は、各runの`screening_rules_hash`に一致するproduction rulesを解決し、market cap・turnover・listing span・required JPX flags・required factsを同じproduction eligibility contractで検査してから、finite `metrics.er_annual`を`er_annual DESC, ticker ASC`で並べた。対象2 runはいずれもmatching rulesを解決でき、common eligible populationとtop20は同一だった。

この補正はmethod採否を意味しない。pure E[r]はReview Set membership/orderのauthorityではなく、同一feasible universeでopportunity costを測るdiagnosticに限る。3y / 5yのmethod-faithful outcomeもまだない。

## 対象と再現条件

- 観測日: 2026-09-01 JST
- application DB: schema 19、SHA-256 `c7d2582b9713670d87f67b5eb2fffdf944d77dc27914490b67a964f5aaa64c28`
- run store: schema 5、SHA-256 `ffc50b4dfc8a3a284f7b9caece1577b079b8ff317fa440689b5757f7c8e15a9d`
- 両storeとも`integrity_check=ok`、`foreign_key_check`違反0
- application DBの全Research Triage 16 revisionsを対象とした。historical v1 15件は変更せず、v2は1件
- operation cycleはdistinct Review Set、v2 Research Triage、human checkpoint/completionをexplicit referenceで接続できるものだけとした。該当するのは`op-20260828-capital-allocation-2`の1件で、現在はhuman Research Set確定待ち。ticker/date近接では補完しない
- approach/supportのfull-fidelity集計はsourceが残るvalidation revisionsの記述に限定し、duplicate observationを独立performance sampleに数えない。run prune済みのrevisionを別runから補完しない
- 集計はread-only SQLite/JSON projectionで一時実行し、新table、event、job、dashboard、stable CLI、集計scriptを残していない

same-universe challengerの再検証対象とprovenance:

| run / Review Set / Triage | matching rules | immutable payload SHA-256 |
| --- | --- | --- |
| `run-revision-6fe887f90b5b4868a12df56f802ae47a` / `review-set-20260828-548f88dc3560` / `research-triage-20260828-current-method-operational-validation` | `496360cbb831f965` / `method/screening/rules/2026-08-30T215359+0900.yaml` | run `9b541facafe79041ee8b2390052ba56a2f69be7736dbd3f1c3d600a5e09969d3`; Review Set `91f895c6dd426e7af89de8f2b4be741c661f5ed5e5ecaa505825c49d9eb19291`; Triage `0772b1c18001442c21d754b6b4a1877afd4058f665d9ff80eec84207c28b1b4a` |
| `run-revision-174755085cd4454bb859428fc222c1cb` / `review-set-20260828-54b964d7791d` / `research-triage-20260828-v2-rollout-validation` | `d7afca967682ed39` / `method/screening/rules/2026-08-31T112223+0900.yaml` | run `398f8a2802a8ace71efbd389d1c0bd1a8c62a5f9c570e8cb3ec13d37c806d272`; Review Set `de782e8251fb4e1f0ef978be0b3a0f5939b9bf3e6f6502d84144b6db77432278`; Triage `19350b31bc42d46060aaf517b8f05821ef581aaee7ac92b45039ca251865d357` |

historical v1 15 payloadのordered aggregate SHA-256は`41f1ae0ed03e3c3385a3d593ffe683d91a1e2d1e0c5f855fd2872cf9cd4ff8cd`。このstudyではstoreを書き換えていない。

## Candidate supply

| 指標 | 観測値 | 読み方 |
| --- | ---: | --- |
| published Triage revisions | 16 | v1 history 15件 + v2 rollout 1件。operation cycle数ではない |
| effective linked v2 operation cycles | 1 | distinct Review Set + v2 Triage + human checkpointをexplicit referenceで接続。activeであり完了標本ではない |
| validation revisions | 3 | 2026-08-28のoperational-validation-corrected / current-method-operational-validation / v2-rollout-validation。独立sampleとして合算しない |
| judged entries | 320 | revision上は各20件。独立judgment標本とは扱わない |
| unique ticker | 57 | 263件は再登場観測 |
| 隣接revision再登場率 | 平均84.0% | 15 publication transition、最小0/20、最大20/20 |
| research / skip | 18 / 302 | all published revisions上の5.6%。operation yieldではない |
| repeat-skip | 240 / 263 = 91.3% | revision間の重複観測。独立cycleのrepeat-skip率には使わない |
| 全320件 E[r]中央値 | 8.450% | 異なるhistorical methodを混ぜた記述統計で、performance比較には使わない |
| 全320件 E[r]負値 | 23 | current-method validation 2 revisionsで16、v2 rollout revisionで7 |

### revision別human judgment

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

full-fidelityで残る2 validation revisionsは同じ20 ticker・同じcompositionを再利用しているため、以下はpublication payloadの断面であり、2 independent performance samplesではない。旧集計との照合のため「1 revisionあたり / 2 revisions合計」で示す。

| 指標 | 1 revision | 2 revisions合計 |
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

pair overlapは1 revisionあたり、Current+Normalized 3、Current+Reinvestment 2、Normalized+Reinvestment 2、Asset+Normalized 1。3-support entryは各pairへ1件ずつ含めた。support-count別ではsingle-support 28件がすべてskip、support=2はresearch 2 / skip 8、support=3はskip 2だった。duplicate validation revisionsなので、approach採否やtarget変更の根拠にはしない。

`all Security Analysis diagnostic`はsource Security Analysis 3,705件（finite E[r] 3,635件）を`er_annual DESC, ticker ASC`で並べ、top20下限8.55%、range 8.55–10.92%、median 8.96%、negative 0だった。この値はReview Setが選択不能な銘柄を含むため、feasible challengerやopportunity costには使用しない。

same-universe pure E[r] challengerはcommon eligible 1,592件（finite E[r] 1,591件）から作り、top20下限7.45%、range 7.45–9.10%、median 8.46%、negative 0だった。`review-set-20260828-548f88dc3560`とのoverlapは0/20、`review-set-20260828-54b964d7791d`とのoverlapは1/20（5021）である。

latest-main v2 rollout revisionは別のlive run / distinct Review Setへ明示接続され、20件中16件が直前revisionから再登場した。support構成はsingle 13、support=2が6、support=3が1。approach別のsupported / unique contribution / research / skipは、Current Earnings Power `7 / 2 / 0 / 7`、Normalized Earnings Power `9 / 4 / 0 / 9`、Asset Value `6 / 4 / 0 / 6`、Reinvestment Value `6 / 3 / 1 / 5`だった。validation publicationでhuman checkpointは未完了のため、独立performance sampleには数えない。同一frozen runに対する回帰testではmembership / order / nominations / analysisが不変である。

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

- **T1 candidate quality**: v2 rollout Review Setはsame-universe pure E[r] top20と1件overlapし、Review Set E[r]中央値は1.685%だった。しかし1 linked v2 operation checkpointと未成熟outcomeでは、どちらが良い候補集合かは未確定
- **T2 stop quality**: 302/320 skipとv2 revisionの7/20 negative E[r]はpublication上で人間gateが多くを止めた事実。永久損失・高値・data defectを正しく止めたかは自由文分類や推測をせず、3y/5y outcomeまで未確定
- **T3 operating value**: all revisions上のresearch 5.6%、repeat-skip 91.3%は重複publicationの影響を含むため、operation performance値には使わない。今回のcutoverで直接観測できたのはResearch prepare required inputs 4→2、source file path 1→0、publish後cross-store dependency 2→1である

## 次の判断条件

performance tuningはexplicit linked v2 operation cycle 6件以上かつv2 judged unique ticker 60件以上になるまで開始しない。production tuningはさらにcurrent methodを忠実に再現する3y/5y cohortを要求する。published revision数やvalidation publicationをcycle分母へ足さず、novelty単独や1 cycleの結果をsuccess判定にしない。

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
