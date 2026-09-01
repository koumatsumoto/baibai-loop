# Candidate Discovery operational value baseline — 2026-09-01

## 結論

このstudyはCandidate Discoveryのalphaやmethod superiorityを証明しない。既存artifactから観測できるのは、15 cycle・300 judgmentが54 tickerへ集中し、Researchへ進めたのは17件（5.7%）、既観測tickerの再判断246件のうち直前も今回もskipだったものが225件（91.5%）という運用実態である。current 4-approach contractを完全に持つ2 cycleでは、Review Setとpure E[r] top20のoverlapは0/20、Review Set E[r]中央値は1.365%、負値は8/20だった。これはCandidate Discoveryが高E[r]順ではないという既存境界を測った結果であり、その良否を確定するものではない。

T1（良い候補供給）は未確定、T2（高値・permanent loss・data defectを止めたか）はjudgment yieldとskipまでは観測可能だがoutcome未接続、T3（人間時間・運用負荷）は低いResearch yieldと高いrepeat-skip、および今回の入力削減から改善余地と改善内容を観測できる。method tuningの開始条件であるv2 6 cycle / 60 unique tickerは、baseline時点で0 / 0であり未達である。

## 対象と再現条件

- 観測日: 2026-09-01 JST
- application DB: schema 19、SHA-256 `773775362b0c568a6225bbd17c541a57089e4a7ac5cbb556890b39bdefafde61`
- run store: schema 5、SHA-256 `df9ba693819a186e47ab8e624e79bcf85f369f42874c64a97841681c7ceac7fd`
- 両storeとも`integrity_check=ok`、`foreign_key_check`違反0
- application DBの全Research Triage 15件を対象とした。全件v1で、historical rowは変更していない
- approach/support/E[r] opportunity-costのfull-fidelity集計は、nominationsとsource Security Analysisが残るcurrent-method 2 cycleだけを対象とした。run prune済みの13 cycleを推測で補完しない
- 集計はread-only SQLite/JSON projectionで一時実行し、新table、event、job、dashboard、stable CLI、集計scriptを残していない

## Candidate supply

| 指標 | 観測値 | 読み方 |
| --- | ---: | --- |
| published Triage cycle | 15 | 全件v1 history |
| judged entries | 300 | 各cycle 20件 |
| unique ticker | 54 | 246件は再登場観測 |
| 隣接cycle再登場率 | 平均84.3% | 14 transition。旧契約からcurrent methodへの1 transitionは0%、同一current Review Setの2 cycleは100% |
| research / skip | 17 / 283 | research yield 5.7% |
| repeat-skip | 225 / 246 = 91.5% | tickerの前回観測も今回もskip。自由文理由の自動分類はしていない |
| 全300件 E[r]中央値 | 8.535% | 異なるhistorical methodを混ぜた記述統計で、performance比較には使わない |
| 全300件 E[r]負値 | 16 | すべてcurrent-method 2 cycle |

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

## Downstream explicit linkage

- `capital_allocation_assessment.research_triage_id`という明示FKで接続できたのは1 Triage / 1 assessmentで、resultは`no_allocation`
- Triageからthesisへの明示foreign/referenceは現行24 thesisに存在しないため、Triage→Thesis件数・buy件数・outcomeは`unavailable`
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

- **T1 candidate quality**: current-method Review Setがpure E[r] top20と0件overlapし、中央値1.365%だった事実はopportunity costを示す。しかし2 duplicate cycleと未成熟outcomeでは、どちらが良い候補集合かは未確定
- **T2 stop quality**: 283/300 skipとcurrent cycleの8/20 negative E[r]は人間gateが多くを止めた事実。永久損失・高値・data defectを正しく止めたかは自由文分類や推測をせず、3y/5y outcomeまで未確定
- **T3 operating value**: research yield 5.7%、repeat-skip 91.5%は人間時間の重複可能性を示す。今回のcutoverはResearch prepare required inputsを4→2、source file pathを1→0、publish後cross-store dependencyを2→1へ減らす

## 次の判断条件

performance tuningはv2 Research Triage 6 cycle以上かつv2 judged unique ticker 60件以上になるまで開始しない。production tuningはさらにcurrent methodを忠実に再現する3y/5y cohortを要求する。rollout後は同じ既存artifact projectionを更新し、novelty単独や1 cycleの結果をsuccess判定にしない。

## Rollout validation

PR B/C merge後のlatest mainによる実運用一巡をここへ追記する。baseline作成時点ではactive v1 Research operationが存在するため、historical/current contractを黙って混在させず、明示的にcompleteまたはabortしてからv2 cycleを開始する。
