---
title: "QARP sector相対割安残差レーンの長期較正"
summary: "quality通過後のsector・size相対割安は候補を作れたが、exact-sector matched boundaryの被覆が全窓で不足したためinsufficientと判定し、productionへ採用しない。"
doc_type: measurement-record
status: active
date: 2026-08-10
---

# QARP sector相対割安残差レーンの長期較正

価値tier: T1 — core E[r] と異なる選択幾何が、core top-20 外から trap 非劣後かつ正の増分価値を持つ候補を安定供給できるかを、production 配線前に判定する。

## 結論

**判定は `insufficient`。production の E[r]、FV、rank、gate、selection payload は変更しない。**

quality gate と sector・size residual は全4窓で候補を作れたが、事前登録した exact-sector matched core boundary の被覆率が 26.2%〜47.8%にとどまり、全窓で floor 75%を外した。3y 2窓は reported pair rows 32件にも届かず、`3y_postcovid` は最多 ticker share 16.8%で上限15%も超えた。不足は未満期 cohort だけではなく比較設計の構造にあるため、QARP 専用 evaluator と test は通常 tree に残さない。別 sector fallback、match pool 拡大、閾値緩和は試さない。

## 実行 identity

- 事前登録 commit: `42526dd78dc15289979b426f3e7d109862199e74`
- cache schema: `13`
- panel / forward: 80 / 80 cohort（2019-11-29〜2026-06-30）
- screening rules hash: `f4a3f3f20838e1cd`（80 panel 全件一致）
- build: panel 80件、forward 1,509,220行、resolved 1,055,260行
- core evaluation SHA-256: `34eb8b8912182c4731cb851df21cdae69fe3415ffcd20e09eb22cfb243432005`
- QARP evaluation SHA-256: `e9abc59daa5435eba5bc6762a43b0c2cdb6ae56e4e6b0d60079c6bf2b96c53b1`

QARP artifact は全 membership、match identity、選択対象の return status、reported / 全損 / 中立の個別 pair と集計、窓別 verdict、overall verdict を持つ。評価後に別 variant、窓、重み、fallback、閾値を試していない。

再構築後の初回 read では、負の売上 TTM から生じた有限な負の `asset_turnover` を reader だけが拒否する不整合を検出した。raw PIT level は符号を保持し、QARP quality gate の `asset_turnover > 0` で候補から除く契約に揃えた。修正後は80 panel / forwardを全件読めることを確認してから同じ固定条件で評価した。

## 事前 coverage

forward outcome を読む前に、既存 panel と `jquants_fin_summaries` だけで測った。`in_population >= 100` の78 panelが対象である。

| field | min | median | 2026-06-30 |
| --- | ---: | ---: | ---: |
| `operating_profit_to_assets` | 67.8% | 80.2% | 88.4% |
| `operating_margin` | 67.8% | 80.0% | 88.2% |
| `asset_turnover` | 72.9% | 84.8% | 94.0% |
| 3列同時 | 67.8% | 80.0% | 88.2% |

全80 membership の内訳は `matched` 27、`match_unavailable` 50、`candidate_unavailable` 3だった。profitability level の coverage 不足を gate 緩和や補完で救済していない。

## 窓別 sufficiency

| window | eligible | candidate | matched / candidate | reported / membership pair | residual / population median | unique QARP / pair | max share | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `1y_design` | 43 | 42 | 11 / 42 (26.2%) | 41 / 45 (91.1%) | 10.35% | 61 / 40 | 13.3% | `insufficient`: match coverage |
| `1y_time_holdout` | 23 | 23 | 11 / 23 (47.8%) | 47 / 47 (100%) | 12.33% | 49 / 37 | 13.9% | `insufficient`: match coverage |
| `3y_all` | 33 | 33 | 9 / 33 (27.3%) | 28 / 37 (75.7%) | 10.44% | 55 / 32 | 14.5% | `insufficient`: match coverage、reported pair < 32 |
| `3y_postcovid` | 19 | 19 | 8 / 19 (42.1%) | 24 / 32 (75.0%) | 11.99% | 30 / 27 | 16.8% | `insufficient`: match coverage、reported pair < 32、concentration |

全窓が effect 判定の前に止まる。候補被覆、residual 比率、unique ticker / pair は大半の窓で floor を満たすため、主なボトルネックは同 sector・rank 11〜30・未使用という比較対象の不足である。

## effect 診断

sufficiency を通らないため、以下を採用根拠には使わない。値は `QARP annualized excess / pair delta / positive share / QARP trap / matched trap / core top-20 trap` の順で、率と効果量は百分率表示である。

### 主読み: QARP ticker 等重み

| window | case | excess | delta | positive | traps: Q / matched / core |
| --- | --- | ---: | ---: | ---: | ---: |
| `1y_design` | reported | +7.91pt | −3.23pt | 45.0% | 6.7% / 10.7% / 9.5% |
|  | total loss | +3.01pt | −3.23pt | 45.5% | 10.6% / 14.8% / 10.7% |
|  | neutral | +3.01pt | −3.26pt | 40.9% | 6.1% / 8.1% / 9.4% |
| `1y_time_holdout` | reported | +12.46pt | +6.93pt | 58.3% | 0.0% / 11.6% / 12.8% |
|  | total loss | +12.46pt | +6.93pt | 58.3% | 0.0% / 11.6% / 13.7% |
|  | neutral | +12.46pt | +6.93pt | 58.3% | 0.0% / 11.6% / 12.7% |
| `3y_all` | reported | +2.15pt | −0.88pt | 50.0% | 18.8% / 17.2% / 12.0% |
|  | total loss | +0.71pt | +2.23pt | 52.6% | 24.6% / 26.3% / 16.4% |
|  | neutral | +0.71pt | −4.00pt | 47.4% | 19.3% / 9.6% / 11.4% |
| `3y_postcovid` | reported | +3.60pt | −0.55pt | 50.0% | 21.4% / 16.1% / 12.1% |
|  | total loss | +5.27pt | +4.65pt | 56.2% | 22.9% / 28.1% / 16.4% |
|  | neutral | +5.27pt | −0.55pt | 50.0% | 22.9% / 8.3% / 11.5% |

### 副読み: cohort 等重み

| window | case | excess | delta | positive | traps: Q / matched / core |
| --- | --- | ---: | ---: | ---: | ---: |
| `1y_design` | reported | +3.69pt | +6.56pt | 63.6% | 4.5% / 12.1% / 11.5% |
|  | total loss | +7.12pt | +6.56pt | 63.6% | 6.4% / 18.2% / 12.7% |
|  | neutral | +7.12pt | +6.56pt | 63.6% | 4.5% / 11.4% / 11.4% |
| `1y_time_holdout` | reported | +13.85pt | +12.70pt | 72.7% | 0.0% / 11.4% / 15.5% |
|  | total loss | +13.85pt | +12.70pt | 72.7% | 0.0% / 11.4% / 15.9% |
|  | neutral | +13.85pt | +12.70pt | 72.7% | 0.0% / 11.4% / 15.5% |
| `3y_all` | reported | +6.15pt | +3.34pt | 55.6% | 13.9% / 19.4% / 13.4% |
|  | total loss | +7.53pt | +44.04pt | 77.8% | 18.9% / 35.6% / 18.9% |
|  | neutral | +7.53pt | +4.46pt | 66.7% | 16.7% / 13.3% / 12.8% |
| `3y_postcovid` | reported | +5.92pt | +0.24pt | 50.0% | 15.6% / 18.8% / 13.8% |
|  | total loss | +8.31pt | +48.77pt | 75.0% | 18.8% / 37.5% / 19.4% |
|  | neutral | +8.31pt | +4.79pt | 62.5% | 18.8% / 12.5% / 13.1% |

ticker 等重みでは design と後半、reported と sensitivity で pair delta の符号が揃わない。cohort 等重みの良い値だけを採らず、月次反復を1銘柄1票へ畳む主読みを維持する。

## 独立検算

評価 module を import せず、panel / forward CSV を標準 CSV reader で読み、quality gate、sector demean、共通 size slope、ordinal percentile、QARP top-20、greedy exact-sector match、population median、annualization、pair delta、trap を再実装した。membership 全体と代表 pair の値が artifact と一致した。

`2021-01-29` / `1y`:

- population 1,404、quality-passed 136、両 residual 123
- match: `9433 → 3765 (selection rank 15)`、ほか `2121→9743`、`9624→6178`、`3636→7860`、`2730→3087`
- population return median −5.6474%、`9433` +15.2647%、`3765` −12.3995%
- QARP excess +20.9121pt、matched excess −6.7521pt、pair delta **+27.6642pt**
- QARP / matched とも trap なし

`2021-12-30` / `3y`:

- population 1,327、quality-passed 267、両 residual 247
- match: `9699→4708`、`4544→2121 (selection rank 19)`、`9678→6541`、`4676→9928`、`9433` は unmatched
- population cumulative return median +15.0679%、annualized median +4.7896%
- `4544` −12.3630%、`2121` +51.3648%、annualized excess −9.0931pt / +10.0280pt、pair delta **−19.1211pt**
- `4544` は trap、`2121` は非 trap

## 誠実性の限定

- 1y forward 窓は calendar 上で重なり、月次 cohort を独立標本とは扱えない。`3y_postcovid` は `3y_all` の部分集合で独立 confirm ではない。
- return は split-adjusted `price_return_only` で、配当を含まない。
- entry が 2020年前後へ偏る3y窓は、日本株上昇局面と COVID 後の regime に依存する。post-COVID感度でも比較対象不足は解消しない。
- profitability level は as-of 以前730暦日の PIT 入力に限る。古い開示を無期限 carry-forwardしない。
- sector 内5行 floor は小 sector を除外する。sector 分類自体の経済的同質性や因果を保証しない。
- match は QARP score 順の greedy without replacement で、global optimal matching ではない。
- 同じ ticker が月次で繰り返されるため、cohort 等重みは集中の影響を受ける。主読みは ticker 等重みである。
- coverage 診断と閾値は forward outcome 前に固定したが、既存 screening の構造を知った上で設計した非盲検 diagnostic である。
- effect 表は構造的 sufficiency failure の下での記述統計で、有意性、因果、将来収益を主張しない。

## Cleanup と production authority

不足は exact-sector match、resolved pair数、集中という構造要因であり、未満期 cohort だけではない。事前登録どおり QARP 専用 evaluator と専用 test を削除し、再評価 taskや配線 issueは作らない。calibration panel の raw `operating_profit_to_assets`、`operating_margin`、`asset_turnover` は、PIT evidenceを失わないため残す。一般 calibration evaluation は per-cohort integrity と core metric status を同じ payload に出し、study が eligibility を自己発行できない fail-closed 境界を保つ。

application DB、market store、production selectionには書き込んでいない。calibration store はローカル正本だけを再構築し、R2へpushしていない。
