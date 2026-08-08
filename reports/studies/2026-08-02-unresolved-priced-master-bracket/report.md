---
title: "未解決 priced-master return の有界 bracket 拡張"
summary: "未解決対象を全損・中立で挟む方向安定契約を採用し、eligibleを3y 30→39、5y 12→18へ増やした。margin_short_to_advはadoption candidate、還元2成分はnegativeと再判定した。"
doc_type: measurement-record
status: active
date: 2026-08-02
---

# 未解決 priced-master return の有界 bracket 拡張

価値tier: T2 — 長期較正で未解決 return が結論を作っていない cohort を有界感度で識別し、最も強い既存 T1 evidence の production 判断を恒久的な data gap から解放する。

## 結論

**未解決 `priced_master_without_universe` row の有界 bracket 契約を採用する。** 報告値では観測済み returnだけを使い、感度側では resolved / unresolvedを問わず全対象へ全損 `-1.0` と置換前のresolved流動性母集団中央値を代入する。core 3 metricの結論方向が報告値と両代入で一致する cohortは、`resolution_complete: false`だけを理由にblockしない。

- 満期済み3y: eligible **30 → 39**（45 cohort中、新規9）
- 満期済み5y: eligible **12 → 18**（21 cohort中、新規6）
- `priced_master_without_universe_return_unresolved`: 3y 10 / 5y 8 → authority blockerから除去
- `priced_master_without_universe_flips_direction`: 3y 1 / 5y 0で不変
- `margin_short_to_adv`: **`adoption_candidate`**。raw annotationのproduction配線は別issueで扱う
- `share_count_reduction_streak` / `dps_guidance_up`: authorityは解消したが効果条件のfailが残るため、両方とも **`negative`**

事前登録は [`2026-08-02-unresolved-priced-master-bracket-preregistration.md`](.../2026-08-02-unresolved-priced-master-bracket/preregistration/report.md) で固定した。特定axisの結果を見て規則、cohort、metric、control、採否条件を変更していない。

## 1. 契約検証

合成fixtureで次を確認した。

- 未解決対象を含んでも `as_reported / total_loss / neutral` の方向が一致すれば、priced-master blockerを立てない。
- 未解決対象の代入で方向が割れれば `priced_master_without_universe_flips_direction` でblockする。
- diagnostics件数とrow同定数の不一致は `priced_master_without_universe_unmeasured`、candidate ticker集合の不一致は `candidate_partition_incomplete`、未知 unresolved statusは `unclassified_unresolved` でfail closedを維持する。
- `resolved_target_count` と `resolution_complete` はcoverage診断として残り、欠けたreturnを観測値として補完しない。

focused testは47件と12 subtests、full testは2,285件がpassした。Ruff format/check、mypy、lint-importsもpassした。

## 2. 全 cohort の前後

2026-08-02時点のproduction storeで、満期済み範囲は3yが `2019-11-29..2023-07-31` の45 cohort、5yが `2019-11-29..2021-07-30` の21 cohortである。直前の容量report以後に3y `2023-07-31` と5y `2021-07-30` が満期へ加わったため、変更前の未解決blockerは9 / 7ではなく10 / 8として同じstoreから再計算した。

| horizon | 満期済み | 変更前eligible | 変更後eligible | 新規eligible |
| --- | ---: | ---: | ---: | ---: |
| 3y | 45 | 30 | **39** | **9** |
| 5y | 21 | 12 | **18** | **6** |

| blocker | 3y 前 | 3y 後 | 5y 前 | 5y 後 |
| --- | ---: | ---: | ---: | ---: |
| `priced_master_without_universe_return_unresolved` | 10 | — | 8 | — |
| `priced_master_without_universe_flips_direction` | 1 | 1 | 0 | 0 |
| `entry_price_gap` | 5 | 5 | 2 | 2 |
| `unpriced_exit_flips_direction` | 1 | 1 | 1 | 1 |

新規eligibleは次のとおりである。

- 3y: `2020-03-31`, `2021-06-30`, `2022-02-28`, `2022-06-30`, `2022-09-30`, `2023-03-31`, `2023-04-28`, `2023-06-30`, `2023-07-31`
- 5y: `2020-03-31`, `2020-09-30`, `2020-10-30`, `2020-12-30`, `2021-02-26`, `2021-07-30`

3y / 5yの `2020-06-30` は方向安定だが `entry_price_gap`、5y `2021-06-30` は方向安定だが `unpriced_exit_flips_direction` が残るためeligibleにはならない。3y `2020-02-28` は対象returnが全件resolvedでも `recommended_rank_top5` の方向が割れるため、priced-master blockerを維持する。

### 未解決対象の方向

各metricの表記は `as_reported / total_loss / neutral`、`+`は比較量 `> 0`、`≤0`は `<= 0` である。列順はtop5 / top10 / E[r]較正。

| horizon / as-of | 対象 / resolved | top5 | top10 | E[r] | 他blocker |
| --- | ---: | --- | --- | --- | --- |
| 3y / 2020-03-31 | 23 / 20 | +/+/+ | +/+/+ | +/+/+ | — |
| 3y / 2020-06-30 | 5 / 4 | +/+/+ | +/+/+ | +/+/+ | `entry_price_gap` |
| 3y / 2021-06-30 | 21 / 19 | +/+/+ | +/+/+ | +/+/+ | — |
| 3y / 2022-02-28 | 8 / 7 | +/+/+ | +/+/+ | +/+/+ | — |
| 3y / 2022-06-30 | 12 / 11 | +/+/+ | +/+/+ | +/+/+ | — |
| 3y / 2022-09-30 | 9 / 8 | +/+/+ | +/+/+ | +/+/+ | — |
| 3y / 2023-03-31 | 13 / 12 | +/+/+ | +/+/+ | +/+/+ | — |
| 3y / 2023-04-28 | 8 / 7 | ≤0/≤0/≤0 | +/+/+ | +/+/+ | — |
| 3y / 2023-06-30 | 16 / 15 | +/+/+ | +/+/+ | +/+/+ | — |
| 3y / 2023-07-31 | 10 / 8 | ≤0/≤0/≤0 | ≤0/≤0/≤0 | +/+/+ | — |
| 5y / 2020-03-31 | 23 / 19 | +/+/+ | +/+/+ | +/+/+ | — |
| 5y / 2020-06-30 | 5 / 4 | +/+/+ | +/+/+ | +/+/+ | `entry_price_gap` |
| 5y / 2020-09-30 | 7 / 6 | +/+/+ | +/+/+ | +/+/+ | — |
| 5y / 2020-10-30 | 7 / 6 | +/+/+ | +/+/+ | +/+/+ | — |
| 5y / 2020-12-30 | 26 / 25 | +/+/+ | +/+/+ | +/+/+ | — |
| 5y / 2021-02-26 | 7 / 6 | +/+/+ | +/+/+ | +/+/+ | — |
| 5y / 2021-06-30 | 21 / 18 | +/+/+ | +/+/+ | +/+/+ | `unpriced_exit_flips_direction` |
| 5y / 2021-07-30 | 10 / 9 | +/+/+ | +/+/+ | +/+/+ | — |

## 3. `margin_short_to_adv` の再判定

独立3y confirmの2 cohortは、新契約でcore 3 metricと`margin_short_to_adv`がいずれもeligibleになった。production CLIは3y-only run全体に `missing_required_horizons:5y` を残すが、required 3y pairは2 / 2 eligibleで、cohort固有のintegrity blockerはない。固定4 as-of × 3y/5yはrequired 8 / 8 eligibleである。

| as-of | 対象 / resolved | priced-master margin方向 | delisting margin方向 | axis n | spread | best excess | trap delta |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| 2023-06-30 | 16 / 15 | stable | stable | 1,220 | +54.86pt | +27.99pt | -27.87pt |
| 2023-07-31 | 10 / 8 | stable | stable | 1,179 | +34.24pt | +9.10pt | -21.63pt |

元の事前登録条件に対する再判定は次のとおりである。

| 条件 | 結果 |
| --- | --- |
| 親evidence | pass |
| 独立3y confirmのintegrity / metric | pass（2 / 2） |
| 独立3yの方向・trap・8 control・標本 | pass |
| 固定3y/5y authority | pass（8 / 8） |
| 固定3y/5yの方向・trap・8 control・標本・coverage | pass |
| 最終判定 | **`adoption_candidate`** |

採用候補は事前登録どおりnullableなraw annotationだけであり、warning、gate、ranking、FV、E[r]、sizingへ変換しない。production surfaceの実装は本変更へ含めない。

## 4. 還元2成分の再判定

独立3y confirmのcore authorityはrequired 3y pairが2 / 2 eligible、固定4 as-of × 3y/5yは8 / 8 eligibleになった。component artifactはpanel、forward、metric、cohortを変更していないため固定hashの既存出力を再利用し、新しいauthorityと元の採否条件を組み合わせて再判定した。

| component | 独立3y | 固定3y | 固定5y | 最終判定 |
| --- | --- | --- | --- | --- |
| H-S `share_count_reduction_streak` | +8.24pt、authority pass | +30.67pt、`dividend_yield` control trap **+1.12pt**でfail | +57.98pt、pass | **`negative`** |
| H-D `dps_guidance_up` | **-9.52pt**、effect/control fail | +4.18pt、pass | **-17.03pt**、effect/control fail | **`negative`** |

authority不足は解消したが、H-Sの固定3y control failとH-Dの独立3y / 固定5yの負方向は変わらない。良いhorizonやauthorityだけで上書きせず、candidate annotationを作らない。

## 5. Artifact と再現

| artifact | SHA-256 |
| --- | --- |
| 全3y/5y評価 | `33762806b989dc0d115b40750c8232df9ceaa3ad45ad33934b63eef28b1865bb` |
| margin独立3y authority | `860a99ebaff64ff28c542d0a82064fcd26b8f2c337ec09518c7a69651ec11271` |
| margin固定3y/5y authority | `aa2be3dc70fa20ef4f2808cc34a86ba54d5a7bfaad22f15730c2d3eeb383e21b` |
| margin独立3y effect | `8d8ad6bbb7e0ee761375924e9e989ad2001f2487cdaea00f76071e5e6f34e6eb` |
| margin固定3y/5y effect | `14e995760ca7f7e8d75be87b7c74bf219b519a7a78b254633d732678644c13c3` |
| component独立3y authority | `bdbb3cf8d388898e253ae738b4eababca023b5601e703c0f19ed612416ce0a00` |
| component固定3y/5y authority | `a3d9e8276ddbbccbf536ef6398af72a16e8b66ef3b324bcc31134bff090aade9` |
| component独立3y effect | `d8d2e50d80feeb45a75634b15b1c93bc66adf731a2fff973add9d3a966c4832d` |
| component固定3y/5y effect | `d97e63b791c631e24ecf1d3704fd44b13284658f5cf98af85b32c9df672a2986` |

authorityは `baibai-engine screening calibration-evaluate` で、全 cohort、独立2 as-of、固定4 as-ofをそれぞれ事前登録したhorizon / required metricへ固定して生成した。component effect artifactは固定hashと入力identityを照合し、再構築や別cohortへの差し替えを行っていない。

## 6. この結果が意味しないこと

両側代入は未解決returnを観測・推定せず、効果量の正確さも保証しない。結論の向きが `-100%` と中立値の範囲で変わらないことだけを答える。eligible増加はscreening methodの有効性、統計的有意性、独立track recordを示さない。`margin_short_to_adv`も空売り主体や因果を識別せず、還元2成分のnegativeは将来の全買戻し・増配施策が無効だという主張ではない。
