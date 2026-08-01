---
title: "質・トラップ複合軸の事前登録"
summary: "E[r] 上位帯を F-score 型の充足本数で層別し、1y/3y の trap と excess を計測する事前登録。"
doc_type: report
status: active
date: 2026-08-02
---

# 質・トラップ複合軸の事前登録

価値tier: T1 — E[r] 上位帯のバリュートラップ率を下げ、深掘り research に入る候補の打率を上げる。

本書は #717 の結果を読む前に、入力列、仮説方向、主検定、時間分割、交絡確認、採否条件を固定する。実装後の数値を見て成分、閾値、符号、窓、統計量を変更しない。逆方向または基準未達の結果は negative とし、別仮説へ読み替えない。

## 1. 成分と composite

各成分は `bool | None` とする。`None` は充足にも不足にも数えない。flow は cohort as-of 以下の開示だけから既存 TTM 合成を行う。前年値は最新開示と同一 `fiscal_period` かつ fiscal year end が1年前の行を既存ロジックで解決し、その行までの履歴だけで TTM と BS carry-forward を再構成する。利益 fallback は current と prior で共通して取得できる最初の `operating_profit` → `ordinary_profit` → `profit` とし、異なる利益概念を比較しない。

| PanelRow field | 充足条件 | 欠損条件 |
| --- | --- | --- |
| `quality_roa_positive` | `profit_ttm / total_assets > 0` | 共通利益 basis、TTM profit、または正の total assets が無い |
| `quality_delta_roa_positive` | current `profit_ttm / total_assets` > prior-year の同値 | current/prior の共通利益 basis または正の total assets が無い |
| `quality_cfo_positive` | `ocf_ttm > 0` | `ocf_ttm` が無い |
| `quality_accrual_healthy` | `accruals_to_assets < 0` | `accruals_to_assets` が無い。0 は不充足 |
| `quality_delta_operating_margin_positive` | current `operating_profit_ttm / sales_ttm` > prior-year の同値 | current/prior の TTM operating profit または正の sales が無い。ordinary/profit fallback は使わない |
| `quality_delta_equity_ratio_positive` | current `equity / total_assets` > prior-year の同値 | current/prior の equity または正の total assets が無い |
| `quality_no_dilution` | `net_share_change_yoy <= 0` | `net_share_change_yoy` が無い |
| `quality_delta_asset_turnover_positive` | current `sales_ttm / total_assets` > prior-year の同値 | current/prior の TTM sales または正の total assets が無い |

`quality_signal_available_count` は非 `None` 成分数、`quality_signal_count` は `True` の本数とする。ただし available count が6未満なら `quality_signal_count = None` とし、複合軸と interaction の母集団から除く。重み、連続スコア、欠損補完は使わない。`AXES` には `AxisSpec(name="quality_signal_count", direction=1)` を固定する。

## 2. 計測集合と統計量

forward return は既存の `price_return_only`、excess は同 cohort の resolved 流動性母集団中央値との差、trap は `excess < -0.20` を使う。

### 軸単独の副検定

既存 axis evaluator が出す以下を 1y / 3y で読む。

- `axes.quality_signal_count.decile_spread_median`
- `axes.quality_signal_count.best_decile_median_excess`
- `axes.quality_signal_count.best_decile_trap_rate`
- aggregate の `mean_rank_ic` / `ic_positive_share`

count の tie は既存 decile 実装の安定順に従い、結果を見て binning を変えない。

### 主検定: E[r] 上位帯 × 質

各 cohort の `in_population` かつ resolved forward return かつ `er_annual` 非欠損の行を `er_annual` 昇順に並べ、既存 decile と同じ境界で最上位 decile を取る。その中で `quality_signal_count >= 5` を high、`<= 3` を low とし、4本は比較から除く。固定列は次のとおり。

- `quality_interaction.er_top_decile_n`
- `quality_interaction.eligible_n`
- `quality_interaction.high.{n,median_excess,mean_excess,trap_rate}`
- `quality_interaction.low.{n,median_excess,mean_excess,trap_rate}`
- `quality_interaction.median_excess_delta` = high − low
- `quality_interaction.mean_excess_delta` = high − low
- `quality_interaction.trap_rate_delta` = high − low

cohort 横断では各 delta の算出可能 cohort の単純平均、`median_excess_delta > 0` の cohort share、high/low の合計 n を使う。有意性検定、年率 track record、grid search は行わない。

## 3. 非重複の時間分割

entry-to-exit の期間が design と confirm の間で重ならないよう、次を固定する。対象 store の cohort 日が月末営業日と異なる場合も ISO 日付の範囲で判定する。

| horizon | design cohort as-of | confirm cohort as-of | 用途 |
| --- | --- | --- | --- |
| 1y | 2019-11-29〜2022-12-30 | 2024-01-31〜2025-06-30 | 主判定。design exit は2023年末まで、confirm entry は2024年以降 |
| 3y | 2019-11-29〜2020-05-29 | 2023-06-30 | 長期方向確認。design exit は2023-05まで、confirm entry は2023-06 |

1y は各窓で high/low 合計 n が各100以上かつ delta 算出可能 cohort が6以上を `eligible` とする。3y design は各群15以上かつ3 cohort 以上、confirm は各群15以上の1 cohort を要求する。未達は不支持ではなく `insufficient` だが、採用可能判定には進めない。

## 4. 交絡確認

主検定と同じ E[r] top decile・high/low を、次の順で**すべて**独立に二分位層別する。各 control の非欠損行を cohort 内中央値で `<= median` / `> median` に分け、両質群が各5行以上ある strata だけを使う。stratum の重みは `min(high_n, low_n)` とし、high−low の stratum delta を重み付き平均する。多変量の交差 strata は top-decile の標本を空にするため作らず、各交絡候補を独立に反証する。

| order | control field | 想定する交絡 |
| --- | --- | --- |
| 1 | `market_cap_oku` | 規模 |
| 2 | `avg_turnover_oku` | 流動性 |
| 3 | `pbr` | value |
| 4 | `per_trailing` | value |
| 5 | `dividend_yield` | value / carry |
| 6 | `price_change_60d` | momentum / reversal |

固定列は `quality_interaction.controls.<field>.{strata_used,matched_weight,stratified_median_excess_delta,stratified_trap_rate_delta}` とする。1y design / confirm の双方で全6 control の2つの delta が算出できることを要求する。途中の control が仮説を消しても後続を省略しない。

## 5. 採否基準

次をすべて満たした場合だけ `adoption_candidate` とする。

1. 1y / 3y の全 eligible cohort aggregate で `axes.quality_signal_count.decile_spread_median` の cohort 平均が正。
2. 1y design / confirm の双方で、主検定の `median_excess_delta >= 0.03` かつ `trap_rate_delta < 0`。
3. 3y design / confirm の双方で、主検定の `median_excess_delta > 0` かつ `trap_rate_delta < 0`。
4. 1y design / confirm の双方で、6 control すべての `stratified_median_excess_delta > 0` かつ `stratified_trap_rate_delta <= 0`。
5. §3 の標本条件と現行 calibration authority/coverage 判定を満たす。coverage blocker の cohort は窓集計から除き、除外数と理由を報告する。

1つでも方向・効果量基準を外せば `negative`、標本または authority が不足すれば `insufficient` とする。`adoption_candidate` でも production gate / ranking / E[r] は変更せず、3y/5y production-decision evidence を事前登録する別 issue に分離する。negative の場合、panel 列は再現可能な計測証跡として残すが candidate metrics / research 表示へ昇格させない。

## 6. 検算と既知の限界

- 単体 fixture で8成分の true / false / missing、available 5で composite が `None`、available 6で0本が0となることを検算する。
- current と prior が異なる利益 fallback しか持たない場合、ΔROA は欠損になることを検算する。
- panel CSV round-trip と cache schema mismatch の fail-closed を検算する。
- 代表 cohort について high/low n、median、trap 件数を独立な短い計算で再計算する。
- 3y confirm は1 cohortだけでレジーム頑健性を主張できない。採用条件を通っても production 参入判断では5yと追加満期 cohortを要求する。
- price-only excess は配当の実現値を含まない。`dividend_yield` 層別は交絡診断であり total-return の因果効果ではない。
