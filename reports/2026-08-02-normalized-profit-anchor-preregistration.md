---
title: "利益正規化anchorとself-range長期化の事前登録"
summary: "3/5 FY平均EPS、cycle peak flag、750/1250/2500 session self-rangeを固定窓で評価する。"
doc_type: report
status: active
date: 2026-08-02
---

# 利益正規化anchorとself-range長期化の事前登録

価値tier: T1 — ピーク利益を正常利益として外挿するE[r]上位候補を識別し、一次リサーチ前の候補品質とFV/E[r] anchorの較正を改善する。

## 1. 目的と変更権限

本計測は次の3仮説を固定して検証する。

- H-1: 3 FY平均EPSによる正規化PERは、当期PERを統制した後も高い側ほど将来returnが劣後する。
- H-2: 当期TTM EPSが直近3 FYレンジの高位にある銘柄は、E[r]上位帯でtrapが多い。
- H-3: self-range上限を750から1250 / 2500 sessionsへ延ばすと、E[r]較正とrecommended replayが改善する。

H-1/H-2はpanel軸とranking-neutralな診断であり、E[r]、FV、selection rulesを変更しない。H-2が全条件を満たした場合だけ、同じfixed flagをcandidate metricとUI warningへ出してよい。H-1のanchor接続とH-3のproduction self-range変更は本issueで行わず、3y/5y `production_decision` evidenceを要求する別issueへ送る。

## 2. outcome非参照で確認した入力coverage

local market storeの入力期間だけを確認した。forward return、既存calibration outcome、仮説軸と将来returnの関係は参照していない。

- financial summary: 2016-08-01〜2026-07-31、FY 62,864行
- daily bar: 2016-08-01〜2026-07-31
- 保存開始からの市場session: 2019-11-29時点815、2022-12-30時点1,568、2025-06-30時点2,178、2026-07-31時点2,443

2500 sessionsの完全履歴は最新cohortにも無い。2500 variantは「利用可能な全履歴を上限2500で読む」診断として実行するが、性能値にかかわらずfull-windowの採用候補にはしない。この制約を後から閾値緩和で消さない。

## 3. H-1/H-2の列定義

### FY EPS集合

各ticker / as-ofについて次の順序で一意に作る。

1. `disclosed_at <= as-of`、`fiscal_period == FY`、`fiscal_year_end`ありの行だけを使う。
2. 同一`fiscal_year_end`の訂正は、as-ofまでで`disclosed_at`が最新の行を選ぶ。最新訂正のEPSがnullなら古い値へfallbackしない。
3. 各FYの`eps_ttm`を、その開示日より後・as-of以前のbar `adjustment_factor`累積でas-of株式基準へ正規化する。
4. fiscal year endが1年ずつ連続する最新3 FY / 5 FYだけを使う。必要年数を一部欠く窓はnullとし、2-of-3や4-of-5へ緩和しない。
5. 赤字、0、黒字をすべて平均へ含める。平均EPSが0以下または非有限なら正規化PERはnullとし、赤字年除外で分母を楽観化しない。

固定列:

- `normalized_per_3fy = asof close / mean(latest consecutive 3 FY adjusted EPS)`
- `normalized_per_5fy = asof close / mean(latest consecutive 5 FY adjusted EPS)`（副診断）
- `eps_cycle_percentile_3fy = (3 FY EPSのうちcurrent TTM EPS未満の数 + 0.5 × 同値数) / 3`
- `eps_cycle_peak_3fy = eps_cycle_percentile_3fy >= 0.8`
- `self_range_observed_sessions = as-of以前にvariant inputが持つ当該tickerのbar本数`

current TTM EPS、3 FY集合のいずれかが欠ける場合、cycle percentile / flagはnullとする。nullをfalseや0へ補完しない。

### 仮説方向とcontrol

- `normalized_per_3fy`: 低いほど良い (`direction=-1`)
- `normalized_per_5fy`: 低いほど良い、副診断のみ
- `eps_cycle_peak_3fy`: `true`が悪い

H-1 controlは順序を固定し、`per_trailing`（必須）、`pbr`、`market_cap_oku`、`sector_33`とする。各controlのstrata内で正規化PERの良い半分−悪い半分を比較する。後から都合の良いcontrolを追加・削除しない。

H-2は`er_annual`がある流動性母集団を昇順に並べた上位decile内だけで、unflagged−flaggedのmedian excessとtrap rateを比較する。flag判定に使えないrowはどちらの群にも入れない。

## 4. H-3 variant契約

grid searchを避け、次の3点だけを比較する。

| variant | self-range cap | bar入力窓 | authority |
| --- | ---: | ---: | --- |
| production baseline | 750 sessions | 1,200暦日 | 現行のまま |
| `self_range_1250` | 1,250 sessions | 2,000暦日 | diagnostic-only |
| `self_range_2500` | 2,500 sessions | 4,000暦日 | diagnostic-only |

各variantは別`--calibration-dir`、variant名・session cap・bar入力窓・authorityを含む別`rules_hash`を使う。diagnostic storeは`production_decision`を拒否し、production storeと混ぜない。self-rangeは現行同様「最大N sessions」であり、100本未満ならanchorを答えない。`self_range_observed_sessions`でfull-cap到達率をcohort別に開示する。

比較する固定指標:

- `er_calibration`: quintileごとのabsolute calibration error平均と、top−bottomのrealized price excess spread
- `recommended_rank_top10`: median excessとtrap rate
- `recommended_rank_top5`:診断として併記するが採否を上書きしない
- variantで推薦tickerが変わったcohort share

## 5. 固定時間分割

H-1/H-2:

| horizon | design | confirm |
| --- | --- | --- |
| 1y | 2019-11-29〜2022-12-30 | 2024-01-31〜2025-06-30 |
| 3y | 2019-11-29〜2021-07-30 | 2022-01-31〜2023-07-31 |

H-3は1250 capを観測し得る断面へ固定する。

| horizon | design | confirm |
| --- | --- | --- |
| 1y | 2022-01-31〜2022-12-30 | 2024-01-31〜2025-06-30 |
| 3y | 2022-01-31〜2022-09-30 | 2022-11-30〜2023-07-31 |

月次cohortのforward窓は重なるため、独立標本数、有意性、track recordを主張しない。price-only excessは各cohortのresolved流動性母集団中央値差、trapは`excess < -0.20`を維持する。

## 6. 採否条件

### H-1 正規化PER

`adoption_candidate`は以下をすべて満たす場合だけとする。

1. 3 FY列のnon-null率が1y design / confirmの各cohort平均で50%以上。
2. 1y design / confirmの両方で、decile spread平均が+3pt以上、positive cohort shareが60%以上。
3. 両1y窓で必須`per_trailing` controlのstratified median spreadが+2pt以上、trap deltaが0以下。
4. PBR、規模、業種controlは両1y窓でmedian spreadが正、trap deltaが0以下。
5. 3y design / confirmの両方でdecile spread平均が正、positive cohort shareが50%以上、`per_trailing` controlもmedian正・trap非悪化。

5 FY列はcoverageと方向を報告する副診断であり、3 FY主判定を上書きしない。

### H-2 cycle peak flag

各cohortでflagged / unflaggedが各5銘柄以上ある場合だけ比較する。`adoption_candidate`は以下をすべて満たす場合だけとする。

1. 1y design / confirmの各窓でeligible合計200以上、比較可能cohortが全resolved cohortの60%以上。
2. 両1y窓でunflagged−flaggedのmedian excess平均が+3pt以上、同trap rate差が−3pt以下。
3. 両1y窓でmedian差が正のcohort shareが60%以上。
4. 3y design / confirmの両方でmedian差が正、trap rate差が0以下。

通過時もranking、E[r]、FVは変えない。candidate/UI warningは`eps_cycle_peak_3fy is true`だけを表示し、nullをwarningへ変換しない。

### H-3 self-range

1250 variantを`adoption_candidate`とする条件:

1. 1y design / confirmの各cohort平均で、流動性母集団の50%以上が1,250 observed sessionsへ到達。
2. 両1y窓で、baseline比のquintile absolute calibration error改善が1pt以上。
3. 両1y窓でtop−bottom realized spreadがbaselineを下回らない。
4. 両1y窓でtop-10 median差が−1pt以上、trap差が0以下。
5. 3y design / confirmで上記4指標が同方向（error改善、spread非悪化、median非劣後、trap非悪化）。

2500 variantはfull-cap coverageが0なので採用不可と事前確定し、方向とpartial-history性能だけを診断として残す。1250が通ってもproduction変更は別issueで3y/5y authorityを満たすまで行わない。

### negative / inconclusive

- 方向または効果量条件を外した仮説は`negative`。
- availabilityまたはgroup sizeだけを外し、効果方向を判定できないものは`inconclusive`。
- designだけ、confirmだけ、5 FYだけ、top-5だけの良い結果で主判定を上書きしない。
- negative後に閾値、FY数、赤字扱い、control、期間、variant点を変更して同じartifactを再採点しない。

## 7. 実装・検算境界

- cache schemaを進め、missing / null / 0 / 負値 / 非有限値 / 不正bool / FY訂正 / 年度不連続 / 分割跨ぎ / variant provenance mismatchをfail closedまたは明示nullにする。
- AP-08へFY平均・cycle flag・self-range observed sessionsのvalidator checklistを追加する。
- fixtureで最新訂正優先、null訂正非fallback、赤字込み平均、平均非正、split、3/5 FY不足、cycle閾値境界、variant store分離を固定する。
- 1 cohortをpanel CSV / forward CSVから別計算し、FY集合、正規化PER、flag group、variant比較を検算する。
- 実装commitを固定してからcacheを再構築し、artifact SHA-256とdated result reportを残す。

## 8. 想定コマンド

```bash
.venv/bin/baibai-engine screening calibration-build \
  --start 2019-11-01 --end 2026-07-31 --force
.venv/bin/baibai-engine screening calibration-build \
  --start 2022-01-01 --end 2025-06-30 \
  --calibration-dir data/screening/calibration-self-range-1250 \
  --panel-variant self_range_1250 --force
.venv/bin/baibai-engine screening calibration-build \
  --start 2022-01-01 --end 2025-06-30 \
  --calibration-dir data/screening/calibration-self-range-2500 \
  --panel-variant self_range_2500 --force
```

H-3が`negative`（`self_range_2500`は採用不可）で確定したため、`self_range_1250` / `self_range_2500` のpanel variantは通常treeに無い。上の2つのbuildを再実行するには、その退役より前のcommitをcheckoutする。

evaluationは§5の8窓をbaseline / 1250 / 2500へ同一指定し、各artifactを別pathへ出す。
