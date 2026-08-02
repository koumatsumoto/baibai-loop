---
title: "3 FY正規化PERのproduction判断面に関する事前登録"
summary: "正規化PERのraw annotationとsector FV anchor replayを分離し、固定3y/5y authorityで採否する。"
doc_type: report
status: active
date: 2026-08-02
---

# 3 FY正規化PERのproduction判断面に関する事前登録

価値tier: T1 — 当期利益で割安に見えるtrapを一次research前に識別し、誤ったFV/E[r]分母へ接続せず候補判断を改善する。

本書は #740 の5y outcomeと新しいanchor replayを読む前に、surface候補、固定cohort、統計量、authority、採否条件を固定する。#721の1y/3y結果は親evidenceとして既知である。同じ結果を再採点せず、追加の3y/5y production scopeだけで権限を判断する。

## 1. 比較するsurface

### H-W: raw annotation

`normalized_per_3fy` を「3期平均EPSに対する現在株価の倍率」というestimateとしてcandidate判断面へ表示する。値をwarningの真偽、gate、rank、E[r]へ変換せず、`per_trailing`と並べて人間が利益cycleを確認する入口に限定する。nullは表示欠損であり、安全や不安全へ補完しない。

production実装は本issueの計測対象外とし、採用条件を通った場合だけ既存candidate metric / shortlist比較表への最小配線を別issueにする。productionで必要となる長期financial input coverageとruntimeは、その実装issueで運用テストする。

### H-A: normalized sector FV anchor

同一cohort・同一共通母集団で、現行 trailing PERのsector anchorと3 FY正規化PERのsector anchorを比較する。

1. panelの`in_population`かつ正の倍率を持つ行から、`sector_33`別medianを計算する。sector n < 10は同じ倍率の市場全体medianへfallbackする。
2. current: `current_sector_upside = sector_median(per_trailing) / per_trailing - 1`
3. normalized: `normalized_sector_upside = sector_median(normalized_per_3fy) / normalized_per_3fy - 1`
4. 両倍率が正の行だけを共通母集団とし、各upside上位decileを比較する。
5. normalized FVは `close * (1 + normalized_sector_upside)` と同値だが、evaluationにはupsideだけを保持する。

これはsector earnings anchor単体のreplayであり、現行E[r]の`min(sector median, self-range median)`、PBRとのblend、carryを再現しない。通過してもE[r]、ranking、既存FVを置換せず、第三の参考FV anchorを検討する別issueへ送る。正規化PERの自己レンジ履歴を新設して現行モデルへ無理に合わせない。

## 2. 固定cohortとauthority

required as-ofは、別のproduction検定で事前選択済みの同じ4点を再利用する。今回のnormalized outcomeを見て選び直さない。

- `2020-01-31`
- `2020-05-29`
- `2021-01-29`
- `2021-05-31`

各as-ofの`3y` / `5y`、合計8組を `--run-purpose production_decision` のrequired scopeとする。required metricはcore 3つに次を加える。

- `normalized_per_3fy`
- `normalized_sector_anchor`

両metricをknown optional metricとしてauthorityへ登録する。delistingと`priced_master_without_universe`の全損 / 中立代入で、`normalized_per_3fy`はdecile spreadの正負、`normalized_sector_anchor`は§5 H-Aの採用条件を満たすかが報告値と両代入で一致する場合だけdirection stableとする。

入力だけの事前確認では、4 as-ofの流動性母集団に対する正の`normalized_per_3fy` coverageは75.48% / 77.84% / 64.52% / 74.75%、currentとの共通行は824 / 909 / 717 / 975件だった。forward returnと5y効果方向は参照していない。

## 3. 固定出力

既存の次を3y/5yで読む。

- `axes.normalized_per_3fy.{n,decile_spread_median,best_decile_median_excess,best_decile_trap_rate}`
- `profit_normalization_hypotheses.normalized_per_3fy_controls.{per_trailing,pbr,market_cap_oku,sector_33}`

H-Aはcohortごとに次を追加する。

- `normalized_sector_anchor.common_n`
- `normalized_sector_anchor.current.{n,median_excess,mean_excess,trap_rate}`
- `normalized_sector_anchor.normalized.{n,median_excess,mean_excess,trap_rate}`
- `normalized_sector_anchor.top_decile_overlap_n`
- `normalized_sector_anchor.changed`
- `normalized_sector_anchor.median_excess_delta`
- `normalized_sector_anchor.mean_excess_delta`
- `normalized_sector_anchor.trap_rate_delta`
- `normalized_sector_anchor.normalized_axis.{n,decile_spread_median,best_decile_median_excess,best_decile_trap_rate}`
- `normalized_sector_anchor.current_axis.{n,decile_spread_median,best_decile_median_excess,best_decile_trap_rate}`

aggregateは4cohortの単純平均、median deltaが正のcohort share、group合計n、changed cohort shareを出す。有意性、独立標本数、track record、閾値探索は主張しない。

## 4. failure mode

- FY平均はcommodity・金融・景気敏感業種のcycle長、利益率変化、事業売却、会計方針、構造的成長 / 衰退をモデル化しない。
- 赤字FYを含む平均EPSが正でも、現在の事業構成に対応する正常利益とは限らない。
- sector medianは同業のcycle同期を共通要因として残す。相対化してもcycle因果は識別しない。
- 3 FY不足、最新訂正null、平均EPS非正、split basis不明はnullのままにする。
- 5yの月次cohortはforward窓が重なる。4 as-ofを4個の独立標本と数えない。
- normalized sector anchorが通っても、self-range・PBR・carryを含むE[r]全体の改善とは読まない。

## 5. 採否条件

### H-W raw annotation

次をすべて満たす場合だけ`adoption_candidate`とする。

1. required 8組すべてでauthorityと`normalized_per_3fy` metric statusがeligible。
2. 3y / 5yそれぞれで、`normalized_per_3fy`のmean decile spreadが正、positive cohort shareが75%以上。
3. 3y / 5yそれぞれで、必須`per_trailing` controlのmean stratified median spreadが正、trap deltaが0以下。
4. PBR、規模、業種controlも両horizonですべてmedian正、trap deltaが0以下。
5. 4 as-of平均の正規化PER coverageが60%以上。

raw annotationは候補を除外・推薦しないため、current PER axisへの優越は要求しない。現在倍率から独立した方向とtrap非悪化を要求する。

### H-A normalized sector FV anchor

次をすべて満たす場合だけ`adoption_candidate`とする。

1. required 8組すべてでauthorityと`normalized_sector_anchor` metric statusがeligible。
2. 3y / 5yそれぞれで、normalized−currentのmean median excess deltaが`>= +0.03`、positive cohort shareが75%以上、mean trap rate deltaが`<= 0`。
3. 3y / 5yそれぞれで、normalized axisのmean decile spreadがcurrent axis以上。
4. 3y / 5yそれぞれでchanged cohort shareが50%以上。順位が実質同じなら新anchorを増やさない。
5. 各cohortの共通母集団100件以上、4 as-of合計500件以上。

H-Aが通ってもE[r]、ranking、既存FVを変更しない。別issueでは第三の参考FVとしての読み手・runtime・production input coverageを先に確認する。

### 総合判定

- 条件を外したsurfaceは`negative`。
- authority、coverage、group sizeだけが不足する場合は`insufficient`。
- H-Wだけ通ればraw annotationだけを候補とし、H-Aの結果を混ぜない。
- H-Aだけ通っても、raw normalized PERを自動warningへ変換しない。
- 良い3yだけ、良い5yだけ、特定cohortだけで採否を上書きしない。

## 6. 実装・検算境界

- evaluationとauthorityのoptional metricだけを追加し、panel/cache schema、production候補、E[r]、FV、rankingは変更しない。
- metric status欠損、未知required metric、全損 / 中立代入の向き割れをnegative testで拒否する。
- anchor replayはsector n < 10 fallback、common row、top-decile境界、overlap、nullをfixtureで固定する。
- 代表cohortをpanel / forward CSVから別計算し、sector median、top-decile ticker、median、trap、overlapを検算する。
- 結果がnegativeなら専用evaluationを通常treeへ残さず、dated reportと再現用commitだけを保持する。
