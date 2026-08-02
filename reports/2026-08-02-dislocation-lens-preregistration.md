---
title: "急落ディスロケーション lens の事前登録"
summary: "低 PER 帯を急落・財務品質で層別し、一時的な売られすぎを機械で識別できるかを 1y/3y forward excess で検定する。"
doc_type: report
status: active
date: 2026-08-02
---

# 急落ディスロケーション lens の事前登録

価値tier: T1 — 一時的な売られすぎを慢性割安・財務毀損を伴う急落から分け、一次 research の着手順位を改善する。

本書は #722 の forward outcome を読む前に、入力、群、出力列、時間分割、交絡確認、採否条件を固定する。結果を見てから急落閾値、quality 条件、割安帯、窓、符号を変更しない。入力 coverage の実現可能性だけを panel から確認し、forward return は参照していない。

## 1. 固定する入力と群

対象は各 cohort の `in_population` かつ正の `per_trailing` を持つ行である。`per_trailing` 昇順の下位20%を低 valuation 帯とする。境界は既存 decile 実装と同じ `int(2 * n / 10)` とし、同値は `(per_trailing, ticker)` の安定順に従う。

- 急落: `price_change_60d <= -0.20`
- 非急落: `price_change_60d > -0.20`
- quality pass: `quality_cfo_positive is True` かつ `quality_no_dilution is True`
- quality fail: 2成分がともに観測済みで、少なくとも一方が `False`
- 欠損: `price_change_60d` または quality 2成分の一方が `None` の行は比較から除く

quality は #717 の point-in-time 成分のうち、営業CF黒字と希薄化回避という異なる2つの財務耐性だけを使う。#717 で negative だった `quality_signal_count >= 5 / <= 3`、重み付き composite、符号反転は使わない。

比較群を次に固定する。

| group | 条件 | 役割 |
| --- | --- | --- |
| `A_acute_quality` | 急落 × quality pass × 低 valuation | 過剰反応候補 |
| `B_chronic_quality` | 非急落 × quality pass × 低 valuation | 主対照。質を揃えた慢性割安 |
| `C_acute_quality_fail` | 急落 × quality fail × 低 valuation | trap 判別対照 |

急落は既存 `PanelRow.price_change_60d` から evaluation 時に導出する。重複する永続 flag、cache schema、production candidate field は追加しない。

## 2. 仮説と統計量

forward return は既存の `price_return_only`、excess は同 cohort の resolved 流動性母集団中央値との差、trap は `excess < -0.20` とする。

- **H1**: A は B より median excess が高く、trap rate が低い。
- **H2**: C は A より trap rate が高い。急落単独ではなく quality 条件が trap を分ける。

cohort ごとの固定列は次とする。

- `dislocation_lens.low_valuation_n`
- `dislocation_lens.eligible_n`
- `dislocation_lens.groups.{A_acute_quality,B_chronic_quality,C_acute_quality_fail}.{n,median_excess,mean_excess,trap_rate}`
- `dislocation_lens.A_minus_B.{median_excess_delta,mean_excess_delta,trap_rate_delta}`
- `dislocation_lens.C_minus_A.{median_excess_delta,mean_excess_delta,trap_rate_delta}`
- `dislocation_lens.controls.<field>.{strata_used,matched_weight,stratified_median_excess_delta,stratified_trap_rate_delta}`

aggregate は群別合計 n、比較可能 cohort 数、delta の cohort 単純平均、`median_excess_delta > 0` の cohort share、control の比較可能 cohort 数と delta 平均を出す。有意性、累積 return、年率、Sharpe、grid search は扱わない。

## 3. 非重複の時間分割

| horizon | design cohort as-of | confirm cohort as-of | 用途 |
| --- | --- | --- | --- |
| 1y | 2019-11-29〜2022-12-30 | 2024-01-31〜2025-06-30 | 主判定。design exit と confirm entry を重ねない |
| 3y | 2019-11-29〜2020-05-29 | 2023-06-30〜2023-07-31 | 長期方向。design exit 後に confirm を開始 |

1y は各窓で A 30件、B 100件、C 20件以上かつ A/B と C/A の比較可能 cohort が各6以上を要求する。3y design は A 15件、B 30件、C 10件以上かつ比較可能 cohort 各3以上、3y confirm は A 5件、B 20件、C 5件以上かつ比較可能 cohort 各2以上を要求する。標本または authority を外す窓は `insufficient` とし、良い数値でも採用根拠にしない。

入力だけの事前確認では 1y design の A/B/C は146/2,705/86、1y confirmは74/1,630/34、3y designは57/372/33、3y confirmは8/221/3だった。したがって3y confirmのC群は現時点で標本条件を満たさない見込みである。この条件を結果後に3件へ緩めない。

## 4. 交絡確認

H1 の A/B を次の順で**すべて**層別する。

1. `realized_volatility_60d` — 高 volatility の反発を急落効果と誤認していないか
2. `market_cap_oku` — 規模
3. `sector_33` — 業種
4. `per_trailing` — 低 valuation 帯の中の水準差

数値 control は cohort 内 quintile、sector は同一 `sector_33` とする。各 stratum で A 1件以上、B 5件以上を要求し、`min(A_n, B_n)` を重みに A−B の median excess と trap rate を集計する。control 同士を交差させない。途中で仮説が消えても後続を省略しない。

## 5. 採否基準

次をすべて満たす場合だけ `adoption_candidate` とし、candidate annotation と OP3 文脈供給を検討する別 issue を起票する。

1. 1y design / confirm の双方で、A−B の mean median excess delta `>= 0.03`、median delta positive share `> 0.5`、mean trap rate delta `< 0`。
2. 1y design / confirm の双方で、C−A の mean trap rate delta `> 0`。
3. 3y design / confirm の双方で、A−B の mean median excess delta `> 0`、median delta positive share `> 0.5`、mean trap rate delta `<= 0`、C−A の mean trap rate delta `> 0`。
4. 1y design / confirm の4 controlすべてで mean stratified median excess delta `> 0`、mean stratified trap rate delta `<= 0`。
5. §3 の標本条件と horizon authority / coverage を全窓で満たす。

標本と authority を満たす窓が方向・効果量を外せば `negative`、方向を評価できても必須窓の標本または authority が不足すれば `insufficient` とする。どちらも production surface は変更しない。negative では dislocation 検出を人間工程に置く。insufficient では同じ閾値の満期データだけを監視し、別閾値へ変更しない。

## 6. 検算と範囲

- fixture で A/B/C、急落境界 `-0.20`、quality 欠損除外、低 PER 20%境界を固定する。
- A/B/C の n、median、trap 件数を代表 cohort の panel / forward CSV から別計算する。
- control は volatility、規模、sector、PER の全列を test し、片群欠損時に数値を捏造しない。
- 3y/5y production rule、E[r]、FV、ranking、screen gate、candidate annotation、UI は変更しない。
- 決算起因 / 非決算起因の分別は履歴 coverage が不足するため本検定へ入れず、将来の独立仮説とする。
