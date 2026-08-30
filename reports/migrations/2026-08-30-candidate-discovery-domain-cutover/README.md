# Candidate Discovery domain cutover rehearsal

価値tier: T1 — 4つの独立した価値源泉からReview Setを作り、調査枠と資本配分判断を分離する。

## 実行境界

- implementation base / `origin/main`: `51f1f3f253107e81bc53403cea35319452b7ba62`
- Issue baselineは上記SHAで、ancestor確認済み。開始時点の追加main差分はない
- 実行日: 2026-08-30 JST
- canonical storeは棚卸しと人間指定のoperation完了以外変更していない
- migration、screening、Review Set、Research Triage、calibrationは独立copyまたは一時storeで実行した
- schema codeをmainへ入れる前のcanonical/R2 cutoverはrepository契約に反するため、owner-attended cutoverとcloud publishはmerge後に行う

## Store migration rehearsal

| store | source | target | source rows | target rows | result |
| --- | ---: | ---: | ---: | ---: | --- |
| application | v18 | v19 | task 97 | task 97 | equal |
| application | v18 | v19 | thesis 24 / review 24 | thesis 24 / review 24 | byte equal |
| application | v18 | v19 | ledger event 25 / price 10 / meta 1 | same | byte equal |
| application | v18 | v19 | historical triage source 13 | Research Triage 13 | ID preserved |
| application | v18 | v19 | position review source 7 | Position Review 7 | ID preserved |
| application | v18 | v19 | allocation assessment source 1 | Capital Allocation Assessment 1 | ID preserved |
| application | v18 | v19 | operation 17 | operation 17 | semantic kind/ref transformed |
| runs | v4 | v5 | run 1 / analysis 3,705 | run 1 / analysis 3,705 | partition equal |
| runs | v4 | v5 | historical machine set 2 | Review Set 0 | raw archive only; no nomination fabrication |

両targetは`integrity_check=ok`、foreign key violation 0。historical machine snapshot 240件をraw archiveへ保存した。

Source backup SHA-256:

- application v18: `96617c2a2af76c315d518c0f54f6205798ce72da5b0a41d27945ea4c689b9257`
- runs v4: `574600b890c9008e99d3a8aa9e779b1f66ee42b4a0b9c89b043070775a94bc5a`

変換payloadごとのsource/target hashとassessment digestは`application-mapping.json`、run partitionは`runs-mapping.json`を正本とする。

### Failure injection

- application schema 17を`expected schema 18, found 17`で拒否
- runs schema 3を`expected schema 4, found 3`で拒否
- 既存targetへのrepeat runを`target paths must not exist`で拒否
- target runtimeのapplication new-writeはv18を拒否し、source `user_version=18`を維持
- target runtimeのruns readはv4を拒否
- model hash、Review Set membership、Research Triage ticker集合、Thesis/review digestの不一致はdomain negative testで拒否

## Actual-data E2E

2026-08-28の完全なmarket storeを使い、Security Analysis 3,705件を生成した。runはTTM quality warningを報告してexit 2となったが、run publication自体は完了した。

- run revision: `run-revision-badade70256e4a52ad9708c873439bf3`
- Review Set: `review-set-20260828-370721df31e6`
- method hash: `ba90c1a6b3f92fcc5eb39bda6fb58dcbe087ffbac876e584c368ff8e8f434b81`
- Review Set YAML SHA-256: `3c0ea2487ee098d3922d25e734b80b99a86469bfa704e298159cc0bc0cdb2282`
- common eligible: 1,592
- approach nomination: 各20
- unique Candidate: 73
- Review Set: 20
- represented: Current 8 / Normalized 8 / Asset 5 / Reinvestment 6
- unfilled target: 全て0
- Review Basis: `research-triage-20260828-owner-skip`（直前の全件skip判断）

owner指定「古いものはskip、3836もskip」を先行operationへ記録してnormal completeした。新Review Setの0件Research Set経路では全20件を`skip`としてcopy DBへ発行した。

- Research Triage: `research-triage-20260828-owner-skip-n10`
- published payload SHA-256: `2dd092d2bfb14630dfc29ec282b8f3f55dfc0a7318a65dd114b3ac51f48fc494`
- research 0 / skip 20 / burned-in machine snapshot 20
- `research prepare`: `actionable=false`、`research_capacity=0`、`note=no_allocation`

Thesis以降のpositive routeは新しい人間判断を捏造せず、source hash、optional E[r]、exact review、allocation、ledger human-confirmation、Position Reviewのcontract testで検証する。

## Calibration replay

method/order/6-5-5-4 targetをfreezeした後、結果依存のparameter変更なしで再構築した。

```text
range: 2019-11-01..2026-07-31
cohort: 81
panel row: 305,367
forward row: 1,527,240
resolved: 1,064,218
control-event exit: 4,737
failure exit: 960
integrity_check: ok
build elapsed: about 38 minutes
evaluation SHA-256: f63ac0768a8b78d766260efa486ae4827c72d8c8a9c795bfe53f7fbf44736050
```

初回replayでcalibration panelから`normalized_per_3fy`が共有Security Analysis builderへ渡らないcoverage defectを検出した。単月smokeでNormalized nomination 20 / multi-support 6を確認後、全期間を再構築した。

| horizon | mature cohort | Review Set top5 median excess | top10 | all | pure E[r] top5 | top10 | top20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3y | 45 | 0.354254 | 0.263541 | 0.232354 | 0.392463 | 0.373999 | 0.381495 |
| 5y | 21 | 0.223177 | 0.332488 | 0.339912 | 0.683319 | 0.631047 | 0.639417 |

3yのsupport>=2は44 cohort、5yは21 cohort。Review Setとpure E[r]の平均top20 overlapは3y 2.2件、5y 1.0件で、E[r]から独立したattention setになっている。

Asset / Reinvestmentの必要入力は2026-05以降の3 cohortだけにあり、3y/5yの成熟cohortには存在しない。このため長期replayのfull representationは0%で、両approachの長期成績は未成熟である。現在データのReview Setでは両targetを充足しているためmethodをfallbackせず、prospective cohortを蓄積する。diagnostic runはproduction authorityを持たず、既存のintegrity/未成熟条件もあるため`production_change_allowed=false`のままである。

E[r]表示contextは、結果値ではなくentry/exit integrityだけでeligibleなrequired as-ofを選び、3y/5y、`review_set_top5` / `review_set_top10` / `er_calibration` / `er_level_calibration`をrequiredにしたproduction scopeから再生成した。artifact SHA-256は`5e334c6fce0b98cc7a6a4ab0868ce5d941182f4e43aaa85772df80938a94deeb`、rules hashは`496360cbb831f965`である。Review Set parameterはreplay結果を見て変更していない。

全期間buildの運用見積りとcompletion-driven waitは[`batch/OPERATIONS.md`](../../../batch/OPERATIONS.md#calibration-全期間-rebuild-の所要時間)へ記録した。

## Independent review

反証レビューで、Normalized Earnings Powerの業種中央値fallbackが既存正本の`n < 10`ではなく
`n < 5`になっている不一致を1件検出した。共有定数`MIN_SECTOR_MEDIAN_POPULATION`へ統合し、9件/10件境界の
negative testを追加した。保存済みpanelからreplayを再評価した結果、上表の長期指標とevaluation hashは不変だった。
実データReview Setは構成銘柄20件を維持したまま順序とsupportが変わったため、上記ID、method hash、artifact hash、
Research Triageを再発行した。解消後のblocking findingは0件。

## Archived evidence

- `application-mapping.json`: row count、payload hash、ledger/thesis equality
- `historical-machine-snapshots.json`: historical judgmentへburn-inされていたraw snapshot
- `runs-mapping.json`: run/analysis partition mapping
- `historical-screening-selections.json`: Nominationを捏造せず退避した旧machine publication
