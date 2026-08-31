# Candidate Discovery v2 actual-data verification

価値tier: T1 — Asset / Reinvestmentの定義に反する候補が有限Review Setを消費する割合を下げ、4つの価値仮説と探索の新規性を維持する。

## 判定

Asset ValueとReinvestment Valueだけをv2へ進める。Current Earnings Power、Normalized Earnings Power、composer、capacity 20、nomination depth 20、representation targets `6 / 5 / 5 / 4`は変更しない。

これはmethod semantic correctionと供給可能性の確認であり、v2のoutperformanceを示さない。Asset / Reinvestmentの3y / 5y full-fidelity outcomeはまだ無いため、将来の経験的revisionはCandidate Discovery decision subjectのauthorityが成立するまで許可しない。

## Frozen input

| item | value |
| --- | --- |
| as-of | 2026-08-28 |
| source run | `run-revision-6fe887f90b5b4868a12df56f802ae47a` |
| source run timestamp | 2026-08-31T00:34:48.959736+09:00 |
| Security Analysis | 3,705 |
| common eligible | 1,592 |
| run store SHA-256 | `df9ba693819a186e47ab8e624e79bcf85f369f42874c64a97841681c7ceac7fd` |
| v1 Review Set canonical JSON SHA-256 | `91f895c6dd426e7af89de8f2b4be741c661f5ed5e5ecaa505825c49d9eb19291` |
| v2 counterfactual canonical JSON SHA-256 | `a37d1483e9091686d1c03eed8eadd4db9462fb4aff8021a680fe90ba87dd2b9c` |
| v1 / v2 method hash | `ddac4f669abed4d4e9d35c1a54d93fd6f0a73ef329c00e454191bbd485d0a7f6` / `ae28cf5636cf01ecaf044c7f26073965f6b85fd202a9e771768df2fd7acc5414` |
| v2 production rules hash | `d7afca967682ed39` |
| v2 calibration method hash | `575a63d930d2caf6` |
| v2 calibration store SHA-256 | `02cf6aa5c3e35dd8406831a8b78845798e64080bff3cdb32f7db2e89481f8ff6` |

source runはread-only SQLite generationとして読み、同じ3,705件をv1 published payloadとcurrent v2 `build_review_set`へ渡した。`PRAGMA integrity_check`は`ok`、source partitionはSecurity Analysis 3,705件、Review Set 1件だった。

current v2 rulesとCandidate Discovery method identityからcalibration snapshotも全再構築した。`PRAGMA integrity_check`は`ok`、2019-11-29..2026-07-31の81 cohort、panel 305,367行、forward 1,527,240行、resolved 1,060,478行で、81 cohortすべてが同じcalibration method hashを持つ。snapshotは一時directoryで検査後にatomic replaceされた。

## Frozen method

### Asset Value v2

- native eligibilityは`asset_backed_ratio > 0`だけ。
- 銀行業、保険業、その他金融業、証券・商品先物取引業を除外。
- `net_cash_to_market_cap`はanalysis contextだけに残し、eligibility / orderから除外。
- orderはasset-backed ratio、PBR sector gap、PBR、equity ratio、ticker。

### Reinvestment Value v2

- v1のpositive P/S、sales growth、FCF yield、operating profit、sales、assets、equity ratio、positive invested capitalを維持。
- `p_s_sector_gap < 0`を要求。
- operating marginとcapital-return proxyの双方を、同じsector 33のmedian以上に限定。
- median populationはcommon eligibleのうちv1 Reinvestment入力が全部成立する534件。
- sector母数が`MIN_SECTOR_MEDIAN_POPULATION`未満の場合だけmarket medianへfallback。境界の10件はsector medianを使う。
- 通過後のorderはv1と同じlexicographic order。

## Actual-data result

| metric | v1 | v2 |
| --- | ---: | ---: |
| Current eligible | 1,533 | 1,533 |
| Normalized eligible | 1,240 | 1,240 |
| Asset eligible | 771 | 674 |
| Reinvestment eligible | 534 | **65** |
| nominations | 各Approach 20 | 各Approach 20 |
| Review Set | 20 | 20 |
| represented count | Current 8 / Normalized 8 / Asset 5 / Reinvestment 6 | Current 7 / Normalized 9 / Asset 6 / Reinvestment 6 |
| target fulfillment | 全充足 | 全充足 |
| v1とのmembership overlap | — | 16 / 20 |
| 2026-08-28より前のTriageに未登場 | 19 / 20 | 18 / 20 |
| Asset top20の金融4業種 | 3 | 0 |
| Reinvestment top20 operating margin median | 0.93% | 4.35% |
| Reinvestment top20 capital return median | 2.66% | 8.08% |
| Review E[r] negative | 8 / 20 | 7 / 20 |
| Review median E[r] | 1.37% | 1.68% |
| Review E[r] range | -4.45%..6.23% | -8.00%..7.48% |

Issue #1153の事前counterfactualはReinvestment eligibleを64件と記録した。実装前に境界を再計算すると、sector母数を`> 10`で判定した場合だけ64件となり、既存`MIN_SECTOR_MEDIAN_POPULATION`契約どおり`>= 10`では65件だった。完了条件は「母数未満だけmarket fallback」「boundaryを含めdeterministic」であり、Normalizedも同じinclusive境界を使うため、65件をcurrent factとする。top20供給、Review membership、target fulfillmentは変わらない。

同様に事前表のReinvestment top20 margin / capital returnは4.58% / 9.42%だったが、凍結した実装契約を3,705件へ再適用した値は4.35% / 8.08%だった。結果に合わせてthresholdや母集団を動かさず、code・fixture・method hashが固定する値を採用する。どちらもv1の0.93% / 2.66%を上回るが、これはeligibilityを定義した同じquality座標上の記述値であり、独立した効果検証ではない。

## Non-authoritative E[r] benchmark

同じcommon eligibleから`er_annual`降順で作ったpure E[r]と、v2 Review orderをtop-N同士で比較した。

| N | overlap | pure E[r] range | pure E[r] median | pure E[r] negative |
| ---: | ---: | ---: | ---: | ---: |
| 5 | 0 | 8.85%..9.10% | 8.92% | 0 |
| 10 | 0 | 8.48%..9.10% | 8.77% | 0 |
| 20 | 1 | 7.45%..9.10% | 8.46% | 0 |

E[r]はReview membership / orderへ戻さない。v2 Reviewは18 / 20の過去Triage未登場を維持する一方、pure E[r]より低い機械期待値を多く含む。探索の新規性とreturn opportunity costは別々に追跡する。

## Calibration authority反証

前回production decisionと同一の固定10 as-ofを変更せず、3y / 5yの20 required cohortへcore 3 metricを要求した。E[r]表示文脈の更新だけは`er_level_calibration`も加えた。

| decision subject | mandatory fidelity | eligible / required | production change |
| --- | --- | ---: | --- |
| `estimator_policy` | core 3 + E[r] level | 20 / 20 | allowed |
| `candidate_discovery_approach: asset-value` | Asset input / nomination / resolved outcome | 0 / 20 | **blocked** |
| `candidate_discovery_approach: reinvestment-value` | Reinvestment input / nomination / resolved outcome | 0 / 20 | **blocked** |
| `candidate_discovery_composer` | 4 Approach + capacity / representation | 0 / 20 | **blocked** |

historical 78 / 81 cohortは同日JPX規制入力が`unavailable`であり、Asset / Reinvestment production入力を持つcohortは長期満期へ達していない。したがって、v2への変更はIssueで凍結したsemantic correctionとcurrent供給確認としてだけ採用し、長期outperformance、composer改善、target最適化の根拠には使わない。E[r] estimator policyとCandidate Discoveryの判断権限は混同しない。

## Verification and monitoring

- Asset金融4業種、net-cash-only、PBR orderingのpositive / negative fixtures。
- ReinvestmentのP/S gap、margin、capital return、sector母数9 / 10境界fixture。
- E[r] / contextを変えてもmembership / order不変。
- v2 method IDs、capacity、depth、targets不変、method hashのsector / median policy binding。
- dated rules revision digest、Review Set recomputation、run-store partition、full repository gate。

### Pre-merge operational rehearsal

canonical market storeはdehydratedなので変更せず、そのcopyをcurrent L1 release `20260830T154511Z-87508ec3-996f46dc35fc`（manifest SHA-256 `c81c81939934cd101ced52b819d0194bc92504e1655027d53a016f735a74cd41`）から一時directoryへhydrateした。446 object / 14,862,343 rowを検証後にatomic replaceし、2026-08-28 coverage gateはcompleteだった。

- new run: `run-revision-bc37e968cc3042a999333d1d55bcc3ba`
- new Review Set: `review-set-20260828-d2b5b0adce63`
- run: universe / Security Analysis `3,705 / 3,705`、既存契約どおりpartial warning exit 2
- Review Set: common eligible 1,592、unique nominations 72、各Approach 20、entries 20、represented `7 / 9 / 6 / 6`、unfilled 0
- run-store `PRAGMA integrity_check`: `ok`
- Review Set YAML SHA-256: `479a6f83ce994dc9e7297f8c0737801d5a941f288365db9b51ecb3ccae2867bb`

merge後はmainのv2 codeで新しいscreening runとReview Setを作る。旧runへv2 Review Setを付け替えない。新Review SetのResearch Triageは別の投資判断operationであり、本変更では発行しない。
