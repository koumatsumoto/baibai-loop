---
title: "未解決 priced-master return の有界 bracket 拡張に関する事前登録"
summary: "対象 return の完全解決を要求せず、全損・中立代入で結論方向が不変な cohort だけを authority 対象にする規則と再判定scopeを固定する。"
doc_type: measurement-record
status: active
date: 2026-08-02
---

# 未解決 priced-master return の有界 bracket 拡張 — 事前登録

価値tier: T2 — 長期較正で未解決 return が結論を作っていない cohort を有界感度で識別し、最も強い既存 T1 evidence の production 判断を恒久的な data gap から解放する。

## 1. 既知の観察と変更動機

現行契約は `priced_master_without_universe` の対象 row を cache 上で同定し、全対象の forward return が resolved の場合だけ全損 `-1.0` と中立値の両側代入を authority 判定に使う。このため満期済み cohort のうち 3y 9件、5y 7件が `priced_master_without_universe_return_unresolved` で blocked である。

既知の下流影響は次のとおりである。

- `margin_short_to_adv` は固定3y/5y authority 8組と効果条件を通過したが、独立3y confirmの `2023-06-30` と `2023-07-31` が対象 return 未解決で `insufficient` になった。
- 株数減少・予想DPS増の独立3y confirmも同じ2 cohortで authority blocked だった。ただし既報の H-S は固定3y control、H-D は独立3yと固定5yの効果条件も外しており、authorityだけで採否を読み替えない。

この動機は既知であり盲検ではない。規則は axis 非依存の integrity contract として固定し、特定 axis の効果量や再判定結果を見て形を変えない。本書を commit するまで変更後の authority artifactを生成しない。

## 2. 固定する integrity contract

対象は、as-of 当日に価格があり同日の master に含まれるが、必要な入力履歴を欠いて universe 評価へ入らなかった `priced_master_without_universe` row とする。

| case | 対象 row の扱い |
| --- | --- |
| `as_reported` | resolved returnだけを現行どおり母集団へ含め、未解決 return は値なしとして除外する |
| `total_loss` | resolved / unresolved を問わず全対象 rowへ `price_return = -1.0` を代入する |
| `neutral` | resolved / unresolved を問わず全対象 rowへ、置換前の resolved 流動性母集団中央値を代入する |

置換は対象 row の returnだけに適用し、valuation metric、rank、E[r]、他の row は変えない。`resolution_complete` と `resolved_target_count` は coverage診断として残すが、単独の authority gateには使わない。

production core 3 metricの向きは既存契約を変えない。

- `recommended_rank_top5`: group `median_excess` が `> 0` か `<= 0`
- `recommended_rank_top10`: group `median_excess` が `> 0` か `<= 0`
- `er_calibration`: 最上位 quintileと最下位 quintileの `median_realized_price_excess` 差が `> 0` か `<= 0`

`as_reported` / `total_loss` / `neutral` の向きが3 metricすべてで一致するときだけ `direction_stable: true` とする。optional required metricの `metric_direction_stable` も同じ3 caseで評価する。1 caseだけ算出不能なら不安定、3 caseすべて算出不能なら感度判定上は安定とし、metric自体の reportability は既存 `metric_statuses` に委ねる。

3y/5y blocker は次へ固定する。

- diagnostics件数と同定 row数が不一致、または感度payloadを測れない: `priced_master_without_universe_unmeasured`
- 両側代入で向きが割れる: `priced_master_without_universe_flips_direction`
- 未解決 returnがあるだけでは blockerを立てない。`priced_master_without_universe_return_unresolved` は authority blockerから除く

候補 ticker集合の不一致、未知 unresolved status、`entry_price_gap`、horizon未満、adjustment factor、delisting感度など他の integrity gateは変更しない。

## 3. 全 cohort への適用と採否

production storeの満期済み3y/5y cohortへ同じ規則を一括適用し、次を dated result reportへ固定する。

- 変更前後の eligible 数
- blocker内訳の前後
- 新規 eligible cohort一覧
- 未解決対象数 / resolved数 / 代入後の方向
- 向きが割れて blockedのままの cohort一覧

次をすべて満たした場合だけ契約を採用する。

1. 未解決対象を含んでも3 caseの向きが一致する合成 cohortが `return_unresolved` で blockされない。
2. 未解決対象の代入で向きが割れる合成 cohortは `priced_master_without_universe_flips_direction` で blockされる。
3. diagnostics件数と row同定数の不一致、candidate partition不一致、未知 unresolved statusは fail closedのままである。
4. 置換対象外の return、rank、E[r]を変更しない。
5. 全満期済み cohortの前後差と計算値を機械生成artifactから再計算できる。

eligible増加数は採否条件にしない。規則が問いへ正しく答え、結果が0件でも契約として採用できる。

## 4. 固定 cohort の再判定

### `margin_short_to_adv`

既報の固定条件を変更せず、次を再評価する。

- 独立3y confirm: `2023-06-30`、`2023-07-31`
- 固定production authority: `2020-01-31`、`2020-05-29`、`2021-01-29`、`2021-05-31` × `3y` / `5y`
- required metrics: core 3 + `margin_short_to_adv`
- 効果、trap、8 control、標本、coverageの採否条件: [`2026-08-02-margin-short-production-preregistration.md`](./2026-08-02-margin-short-production-preregistration.md)から変更しない

元reportの「完全解決時だけ再判定」は、本事前登録で integrity contractを両側代入へ置き換えたことを明記して再判定する。独立2 cohortが新契約でeligibleかつ元の全条件を満たす場合だけ `adoption_candidate`、方向・効果条件の未達は `negative`、authorityまたは標本だけの未達は `insufficient` とする。

### 株数減少・予想DPS増

同じ2 cohortと固定4 as-of × 3y/5yを、[`2026-08-02-share-return-components-production-preregistration.md`](./2026-08-02-share-return-components-production-preregistration.md) の metric、control、標本、採否条件を変えずに再評価する。authorityがeligibleになっても、H-Sの固定3y `dividend_yield` trap条件とH-Dの独立3y / 固定5yの負方向を上書きしない。元条件どおり各成分を `adoption_candidate / negative / insufficient` に判定する。

## 5. 実装と検算境界

- cache schema、panel build、forward build、production candidate、UI、ranking、FV、E[r]は変更しない。
- 変更面は感度verdict、authority blocker、negative test、現行contract docに限定する。
- 既存artifactを同じ固定scopeで再評価し、再構築や別cohortへの差し替えを行わない。
- 全 cohort reportと2つの固定再判定reportにartifact SHA-256、実行条件、authority、採否を記録する。
- 有意性、独立標本、track record、欠けた実現returnの推定値は主張しない。
