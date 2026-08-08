---
title: "株主還元の変化軸の計測結果"
summary: "低 PER 帯の還元変化 composite は事前登録条件を満たしたが、安定した独立軸はグロス株数減少に限られるため production 採用は追加長期検証へ分離する。"
doc_type: report
status: active
date: 2026-08-02
---

# 株主還元の変化軸の計測結果

価値tier: T1 — 低 valuation 候補のうち還元が改善している銘柄を識別し、バリュートラップ率を下げる。

## 結論

**判定は `adoption_candidate`。ただし本 issue では panel 証跡だけを残し、candidate metrics、research annotation、gate、ranking、E[r] は変更しない。**

低 PER 20%帯の `shareholder_return_change` true−false は、1y design で median excess +6.44pt / trap rate −6.46pt、非重複の1y confirmで +4.06pt / −9.03pt、3y designで +7.73pt / −12.57ptだった。1y両窓の全6 controlも median正・trap非正を満たし、標本・authority条件も通過した。

ただし、効果を「増配全般」や「自社株買い実施」の予測力とは読まない。連続軸の `dps_yoy_latest` は3窓すべて逆方向で、`share_count_reduction_streak` だけが3窓すべて正だった。後者は自己株式込みのグロス株数減少であり、自己株取得の一次事実ではない。production参入判断は、株数減少と予想増配を分離し、一次IRで取得・消却を照合したうえで、独立3y confirmと5yを固定する別 issue に送る。

## 1. 事前登録と実行契約

入力境界、列、方向、低 PER 帯、時間窓、交絡順序、採否条件は、結果計測前の commit `de0fd1d40dcddfd7893591239fbbfc9884369f98` と [`2026-08-02-shareholder-return-change-preregistration.md`](.../2026-08-02-shareholder-return-change/preregistration/report.md) で固定した。実装は計測前の commit `8a04cb8d777880fe082bfa736b49944a24feea85` で固定した。

実行コマンド:

```bash
.venv/bin/baibai-engine screening calibration-build \
  --start 2019-11-01 --end 2026-07-31 --force
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 1y --horizon 3y \
  --out /tmp/shareholder-return-change-eval.yaml
```

- cache schema: `7`
- panel: 80 cohort。全headerが新6列を持ち、reader round-tripを通過
- forward rows: 1,509,220（resolved 1,055,260）。schema v6時点と一致し、既存forward母集団は不変
- evaluation SHA-256: `40e83d653bed8b01db3ca3195ec9c3fb21193d2c3cbc9ada8db25ad969d48f04`
- metric: cohort の resolved 流動性母集団中央値に対する `price_return_only` excess
- trap: `excess < -0.20`

既存 `FIN_INPUT_WINDOW_DAYS=730` から作る `FinancialSnapshot` と E[r] は変更していない。還元変化列だけが calibration panel の1200日補助履歴を使う。

## 2. availability

80 panel の `in_population` 107,802 row について、欠損を false や0へ補完していない。

| field | non-null rows | coverage | 補足 |
| --- | ---: | ---: | --- |
| `dps_streak_up` | 81,289 | 75.41% | true 64,566 / false 16,723 |
| `dps_yoy_latest` | 86,146 | 79.91% | prior 0→正は initiation へ分離 |
| `dps_guidance_up` | 86,567 | 80.30% | true 43,552 / false 43,015 |
| `dividend_initiation` | 87,782 | 81.43% | true 2,674（観測可能行の3.05%） |
| `share_count_reduction_streak` | 86,754 | 80.48% | 0回 75,171 / 1回 7,225 / 2回 4,358 |
| `shareholder_return_change` | 85,864 | 79.65% | true 68,118 / false 17,746。判定可能行の79.33%がtrue |

composite はtrue側が広く、hard gateにすると差別化より母集団の大半を通す形になる。今回の判定は「追加のproduction検証へ進める」であり、現行screeningへそのまま接続してよいという意味ではない。

## 3. 主検定

1y は leading evidence として resolved cohortを使う。3y は master、survivorship、adjustment factor、input range、candidate partition、universe未評価、entry gap、delisting sensitivity の現行authority条件を満たすcohortだけを集計した。

| window | requested / eligible | delta cohorts | change n / no-change n | mean median delta | positive cohort share | mean trap delta | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1y design | 38 / 38 | 38 | 5,062 / 1,265 | **+6.44pt** | 76.32% | **−6.46pt** | pass |
| 1y confirm | 18 / 18 | 18 | 3,428 / 324 | **+4.06pt** | 72.22% | **−9.03pt** | pass |
| 3y design | 7 / 4 | 4 | 512 / 100 | **+7.73pt** | 75.00% | **−12.57pt** | pass |

3y design の eligible は2019-12-30、2020-01-31、2020-04-30、2020-05-29。除外理由は `entry_price_gap`、`priced_master_without_universe_flips_direction`、`unpriced_exit_flips_direction`、`priced_master_without_universe_return_unresolved` 各1件で、一部cohortは複数理由を持つ。

各窓は事前登録した標本条件を満たす。1y両窓は各群100件以上・6 cohort以上、3y designは各群15件以上・3 cohort以上である。

## 4. 交絡確認

事前登録順に6 controlをすべて適用した。値はcohortごとの二分位 strata 内 change−no-changeを `min(change_n, no_change_n)` で重み付けし、そのcohort値を窓内で単純平均したもの。

| control | design cohorts / weight | design median / trap delta | confirm cohorts / weight | confirm median / trap delta |
| --- | ---: | ---: | ---: | ---: |
| `dividend_yield` | 38 / 1,153 | +6.36pt / −3.20pt | 18 / 240 | +5.06pt / −5.53pt |
| `per_trailing` | 38 / 1,265 | +5.08pt / −6.49pt | 18 / 314 | +4.85pt / −8.58pt |
| `pbr` | 38 / 1,248 | +8.59pt / −7.39pt | 18 / 316 | +6.57pt / −10.78pt |
| `market_cap_oku` | 38 / 1,261 | +6.09pt / −6.45pt | 18 / 313 | +4.19pt / −8.30pt |
| `avg_turnover_oku` | 38 / 1,257 | +5.96pt / −6.59pt | 18 / 317 | +7.47pt / −9.92pt |
| `price_change_60d` | 38 / 1,258 | +6.01pt / −6.13pt | 18 / 313 | +5.46pt / −9.04pt |

1y design / confirm の全controlが採否条件を通過した。3y designは採否条件外の診断として5 controlでmedian正だったが、`price_change_60d` 層別だけ −1.86ptだった。長期効果をmomentum/reversalから完全に分離できたとは主張しない。

## 5. 連続軸と個別成分

### 連続軸

| axis | 1y design spread / positive share | 1y confirm | 3y design | 判定 |
| --- | ---: | ---: | ---: | --- |
| `dps_yoy_latest` | −3.96pt / 28.95% | −1.05pt / 33.33% | −7.83pt / 0.00% | 仮説と逆。採らない |
| `share_count_reduction_streak` | **+4.84pt / 89.47%** | **+10.66pt / 94.44%** | **+11.40pt / 100%** | 3窓で正。追加検証へ進める |

事前登録条件4は2軸の少なくとも一方が全窓で正であることを要求しており、株数減少streakが満たした。DPS YoYの逆方向を、compositeの良い結果で打ち消してDPS増配の支持とは読まない。

### bool成分の低 PER 帯 true−false

| component | 1y design n true / false・median / trap delta | 1y confirm | 3y design | 解釈 |
| --- | --- | --- | --- | --- |
| `dps_streak_up` | 4,395 / 1,572・−2.12pt / +1.64pt | 3,029 / 537・+4.01pt / −8.74pt | 470 / 112・−3.42pt / +4.41pt | 時間一貫性なし |
| `dps_guidance_up` | 2,677 / 3,365・+4.58pt / −2.28pt | 2,177 / 1,407・+5.04pt / −2.79pt | 244 / 337・+8.22pt / −9.93pt | 方向は一貫。ただしcomponent単独の事前登録採否ではない |
| `dividend_initiation` | 202 / 6,241・+7.40pt / +9.57pt | 165 / 3,499・−12.66pt / +13.22pt | 3 / 628・+1.93pt / −12.80pt | 稀で不安定。採らない |

## 6. 独立検算

`2024-01-31` の1y cohortをpanel/forward CSVから別計算した。population median return −0.4143%、低 PER帯226件、composite判定188件、change 174 / no-change 14。change median excess −1.0113%、no-change −0.8209%、delta −0.1904ptでevaluation YAMLと丸め前まで一致した。trap件数はchange 24 / no-change 4で、rate差 −14.78ptとも一致した。

単体fixtureでは、最新revisionのnullから旧値へ戻らないこと、3 FY不足、DPS 0→正、予想増配、株数streak 0/1/2、実績FY間と最新開示後の1:2分割を検算した。cache readerはoptional boolの不正token、非有限DPS YoY、streak範囲外、compositeと成分の矛盾を拒否する。

## 7. 判定表

| 事前登録条件 | 観測 | 判定 |
| --- | --- | --- |
| 1y design / confirm: median ≥ +3pt、trap < 0、positive share > 0.5 | +6.44 / +4.06pt、trap負、76.32 / 72.22% | pass |
| 3y design: median > 0、trap < 0、positive share > 0.5 | +7.73pt、−12.57pt、75.00% | pass |
| 1y両窓の全6 control: median > 0、trap ≤ 0 | 12組すべて通過 | pass |
| 2連続軸の少なくとも一方が全窓でspread正 | 株数減少streakが +4.84 / +10.66 / +11.40pt | pass |
| 標本・authority | 1y両窓、3y design 4 cohortが条件充足 | pass |

総合判定は `adoption_candidate`。これはproduction変更の許可ではない。次段階は次を固定する別issueとする。

1. `share_count_reduction_streak` と `dps_guidance_up` を分離し、compositeのどの成分が独立寄与を持つかを事前登録する。
2. グロス株数減少を会社IRの自己株取得・消却・増資と照合し、「買戻し」と「その他の株数変化」を区別する。
3. 独立3y confirmと5yを要求し、3yのmomentum層別で見えた逆方向を再確認する。
4. 通過しても最初のsurfaceはcandidate annotationに限定し、gate / ranking / E[r] は別採否とする。

## 8. この結果が意味しないこと

- `share_count_reduction_streak` は自己株取得の直接観測ではない。自己株式込みグロス株数の減少であり、消却・発行・組織再編の純効果を含む。
- `price_return_only` は実現配当を含まない。増配によるtotal shareholder returnを直接測った結果ではない。
- composite trueは判定可能行の79.33%で、少数の高確信候補を選ぶ軸ではない。production surfaceでは閾値・表示価値を改めて問う。
- 3yはdesign 4 cohortで、独立confirmではない。長期再現性は未確定である。
- DPS YoY、3期非減配、還元開始を単独で採用する根拠ではない。結果を見てから良い成分だけを現行rulesへ接続しない。
