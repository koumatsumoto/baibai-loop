---
title: "空売り残/ADVの長期production判断に関する事前登録"
summary: "独立3y confirm、固定3y/5y authority、survivorship感応度でraw annotationの採否を固定する。"
doc_type: report
status: active
date: 2026-08-02
---

# 空売り残/ADVの長期production判断に関する事前登録

価値tier: T1 — 高い空売り残/ADVと将来劣後の関連を一次research前に示し、バリュートラップへ調査枠を使う確率を下げる。

本書は #738 の新しい3y confirmと5y outcomeを読む前に、surface、cohort、統計量、authority、採否条件を固定する。#720 の1y design / confirmと3y designは親evidenceとして既知であり、同じ窓を再採点しない。方向または効果量の未達は `negative`、authorityまたは標本だけの未達は `insufficient` とする。

## 1. 対象metricとsurface

`margin_short_to_adv` は、貸借銘柄の最新公表short残を as-of 以前20取引日の平均出来高株数で割った日数相当の raw fact である。低いほど良い方向に固定する。信用銘柄の構造的short 0、ADV欠損・非正、ADV窓内の分割・併合は `null` とし、0や別期間で補完しない。

採用候補は **raw annotationだけ** とする。候補判断面では `空売残/ADV` の日数相当を既存の信用需給factと並べ、値をwarningの真偽、gate、ranking、FV、E[r]、sizingへ変換しない。thresholdを検証していないため、research warningやhard gateは候補にしない。production配線は本issueの計測対象外とし、採用条件を通った場合だけ別issueへ送る。

空売り主体、借株コスト、貸株料、取引理由は観測しない。関連が成立しても、空売り主体の情報優位や企業価値悪化の因果は主張しない。

## 2. 独立3y confirm

#720 の3y designは `2019-11-29..2020-05-29` で、最後のforward exitは2023年5月である。entryがその後となり、かつ2026-07-31までに3y horizonが満了する次の2 cohortを独立confirmに固定する。

- `2023-06-30`
- `2023-07-31`

各cohortの `integrity_status`、`metric_statuses.margin_short_to_adv`、`axes.margin_short_to_adv`、全controlを読む。2件を独立標本数とは呼ばず、非重複時間窓で方向を反証するconfirmとして扱う。

## 3. 固定3y/5y production authority

required as-ofは、別のproduction検定で結果を見る前に選択済みの同じ4点を再利用する。今回のmargin outcomeで選び直さない。

- `2020-01-31`
- `2020-05-29`
- `2021-01-29`
- `2021-05-31`

各as-ofの `3y` / `5y`、合計8組を `--run-purpose production_decision` のrequired scopeとする。required metricはcore 3つに `margin_short_to_adv` を加える。

`margin_short_to_adv` をknown optional metricとしてauthorityへ登録する。delistingと`priced_master_without_universe`の全損 / 中立代入について、次のcohort-level判定がas-reportedを含む3ケースで一致する場合だけdirection stableとする。

```text
axis_adoption_pass = decile_spread_median > 0
                     and best_decile_trap_rate <= worst_decile_trap_rate
```

metricの算出可否は `metric_statuses` が担う。どのケースでも軸が算出不能ならstableと補完せず、required metricのunresolvedとして遮断する。

## 4. 固定出力と集計

cohortごとに次を読む。

- `axes.margin_short_to_adv.{n,rank_ic,best_decile_median_excess,best_decile_trap_rate,decile_spread_median,deciles}`
- `margin_supply_demand_hypotheses.margin_short_to_adv.controls.<control>.{strata_used,matched_weight,stratified_median_excess_spread,stratified_trap_rate_delta}`
- `coverage.delisting_exclusion.metric_direction_stable.margin_short_to_adv`
- `coverage.priced_master_without_universe.metric_direction_stable.margin_short_to_adv`

controlは #720 と同じ8本を順序も変えずすべて使う。

1. `market_cap_oku`
2. `avg_turnover_oku`
3. `per_trailing`
4. `dividend_yield`
5. `close`
6. `price_change_60d`
7. `realized_volatility_60d`
8. `sector_33`

各window / horizonではcohortを単純平均し、次を固定列として報告する。

- cohort数、axis合計n、`margin_short_to_adv` coverage
- mean decile spread、positive cohort share
- mean best decile median excess
- mean `(best_decile_trap_rate - worst_decile_trap_rate)`
- 各controlの利用可能cohort、matched weight合計、mean stratified median spread、mean stratified trap delta

有意性検定、p値、独立標本数、累積track recordは出さない。

## 5. 採否条件

次をすべて満たす場合だけraw annotationを `adoption_candidate` とする。

### 独立3y confirm

1. 固定2 cohortが両方ともintegrity / metric eligible。
2. mean decile spread `> 0`、positive cohort share `= 100%`。
3. mean best−worst trap delta `<= 0`。
4. 全8controlでmean stratified median spread `> 0`、mean stratified trap delta `<= 0`。
5. axis合計n `>= 1,500`。各controlは2 cohortとも利用可能で、matched weight合計 `>= 200`。

### 固定3y/5y authority

1. required 8組すべてでauthorityと`margin_short_to_adv` metric statusがeligible。
2. delisting / priced-masterのmetric directionが8組すべてstable。
3. 3y / 5yそれぞれでmean decile spread `> 0`、positive cohort share `>= 75%`。
4. 3y / 5yそれぞれでmean best−worst trap delta `<= 0`。
5. 3y / 5yそれぞれの全8controlでmean stratified median spread `> 0`、mean stratified trap delta `<= 0`。
6. 各horizonのaxis合計n `>= 2,500`。各controlは4 cohortとも利用可能で、matched weight合計 `>= 400`。
7. 4 as-of平均の正の`margin_short_to_adv` coverageが各horizon60%以上。

親evidence、独立3y confirm、固定3y/5yのどれか1つでも外せばproduction面を変更しない。良い3yだけ、良い5yだけ、特定cohortだけ、controlの一部だけで上書きしない。

## 6. 実装・検算境界

- evaluationのoptional metric statusとsurvivorship sensitivityだけを追加し、panel/cache schema、production candidate、UI、rules、E[r]、FV、rankingは変更しない。
- metric status欠損、未知required metric、全損 / 中立代入によるadoption判定反転をnegative testで拒否する。
- 貸借銘柄限定、欠損非補完、point-in-time weekly margin境界は既存panel契約を変更しない。
- 代表cohortをpanel / forward CSVから別計算し、axis n、decile境界、spread、best / worst trap、全controlの少なくとも1本を検算する。
- artifactのSHA-256、authority、標本、判定表、監視事項をdated reportへ固定する。
- 結果がnegativeなら専用production surfaceを作らない。採用候補なら最小表示を別issueへ切り出す。
