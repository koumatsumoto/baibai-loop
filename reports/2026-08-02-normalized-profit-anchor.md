---
title: "利益正規化anchorとself-range長期化の計測結果"
summary: "3 FY正規化PERは独立したtrap識別候補、cycle peak flagと1250/2500 self-rangeは不採用と判定した。"
doc_type: report
status: active
date: 2026-08-02
---

# 利益正規化anchorとself-range長期化の計測結果

価値tier: T1 — ピーク利益を正常利益として外挿するE[r]上位候補を識別し、一次リサーチ前の候補品質とFV/E[r] anchorの較正を改善する。

## 結論

- **H-1 `normalized_per_3fy` は `adoption_candidate`。** 1y design / confirmのdecile spreadは+17.28pt / +19.36pt、positive shareは86.84% / 77.78%。当期PER統制後もmedian spreadは+5.57pt / +4.11pt、trap差は−9.74pt / −6.98ptで、3y design / confirmも同方向だった。本issueではE[r]・FV・rankingへ接続せず、production anchorまたはwarningの長期authority検証を別issueへ送る。
- **H-2 `eps_cycle_peak_3fy` は `negative`。** 1y designはunflagged−flagged median +3.25ptだったがpositive share 59.46%、trap差+1.13pt。confirmは+1.85pt、trap差+0.57ptで効果量とtrap条件を外し、3y confirmはmedian −12.69ptと逆転した。candidate/UI warningは追加しない。
- **H-3 `self_range_1250` は `negative`。** 1y designのcalibration errorは悪化し、confirmではtop−bottom spread −2.64pt、top-10 median −3.63pt、trap +7.77pt。3y confirmもspreadとtrapが悪化した。production 750 sessionsを維持する。
- **`self_range_2500` はfull-cap coverage 0で採用不可。** partial-history診断でも1y confirmのtop-10 median −10.23pt、trap +15.00ptとなり、不足coverageを無視して採らない。

H-1の結果は「FY平均EPSをそのままFV分母に採用してよい」という結論ではない。正規化PERが当期PERとは別のtrap識別座標になり得る、という候補抽出上の証拠である。

## 1. 事前登録と実行契約

定義、赤字扱い、control、時間分割、採否条件は、outcome計測前のcommit `6149e37` と [`2026-08-02-normalized-profit-anchor-preregistration.md`](./2026-08-02-normalized-profit-anchor-preregistration.md) で固定した。panel / evaluation実装はcommit `1795d23`、長期split確認をadjustment eventだけへ限定するI/O契約はcommit `64a91ca`で固定した。長期split eventをnormalized profitへ、通常履歴barをshareholder-returnへ渡す配線と、直近bar窓より前のsplitを固定するfixtureはcommit `68decc5`で検証し、その状態から全storeを再構築した。

- cache schema: `9`
- production: 80 panels、forward 1,509,220行、resolved 1,055,260行
- 1250 / 2500: 各42 panels、forward 801,030行、resolved 541,789行
- 3 store間で同一期間のforward件数は一致
- price-only excess: cohortのresolved流動性母集団中央値差
- trap: `excess < -0.20`

artifact SHA-256:

| store / window | SHA-256 |
| --- | --- |
| production H1/H2 1y design | `362ae49a9f6a5373c778460b6d12271440cca47fb503719ae304bd5cb9cfab87` |
| production H1/H2 1y confirm | `e3bd519116938ee40501613c0efdd7c26e7fa13ca3e4dc276e7c00e1ac1c48aa` |
| production H1/H2 3y design | `6a98b06803805cfe7c49c1db589647e1320171ca4a506506b0d2f0a38f6e5d10` |
| production H1/H2 3y confirm | `f8498d6c21604a0865ac2dc6e5018cac60642b5af6d43671b2f8b8e07daacc14` |
| production H3 1y design | `202bcb91d10e56aee62898aadaa1a742f9c5ff9ac8b9d1e0e5b2439a9dc218f6` |
| production H3 1y confirm | `e3bd519116938ee40501613c0efdd7c26e7fa13ca3e4dc276e7c00e1ac1c48aa` |
| production H3 3y design | `75bc36986f17c139bd12b641d350bdde2109cd49703e255a558f8d04b46a230d` |
| production H3 3y confirm | `c2b4438dad811eb2a3b72f3900c2c41a6fac4bbaa1c99d4ca1c4193798aaced6` |
| 1250 1y design / confirm | `d3bc541609ae816ffff3a571c13d409d8ec199df9d5ce8f25cda13278328d184` / `36f087b0a41de179dfa66dd1382c7d6e319e1c320e1a6fffacfce710448dec6b` |
| 1250 3y design / confirm | `b90e0c00b41bdea8a49ad59aee83752fa9d194ebf123d1599977dd902811e6a2` / `34e024a73aef2f8b8427d8a3d007e4c9c67f0c64e99cae525337a4cb636381db` |
| 2500 1y design / confirm | `a635d17a6fd177cfb0f28909b260e6c97b51c0d1d97281cf545d6a864faf00ee` / `d511f36700328fa6da38b3ed2fe4754a690ec6536e96286ccc281c80cfffb442` |
| 2500 3y design / confirm | `7e0d2ed8b5fcf5cb7d0eb7eb0dcc0126811f4c45d3e89148f1dd30a644750036` / `179af616376a0bd261bcd71690062a3de7e54a57945b524efe9c0077c0022d72` |

## 2. 入力とavailability

FYはas-ofまでの最新訂正を年度末ごとに1行へ畳み、各EPSをas-of株式基準へ調整した。赤字と0を平均へ含め、連続年不足、最新null訂正、平均EPS非正をnullにした。

| window | 3 FY PER coverage | 5 FY PER coverage |
| --- | ---: | ---: |
| 1y design | 70.92% | 37.02% |
| 1y confirm | 76.16% | 69.48% |
| 3y design | 71.57% | 14.02% |
| 3y confirm | 71.95% | 66.87% |

3 FYは全窓で50%下限を超えた。5 FYは早期cohortで細いため、副診断のまま主判定を上書きしない。

self-rangeのfull-cap到達率は1250 storeで1y design 89.96%、confirm 90.53%、3y design 90.30%、confirm 89.52%。2500到達率は全窓0だった。

## 3. H-1: 3 FY正規化PER

低い正規化PERを良い側とする。

| window | cohort | n | mean decile spread | positive share | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1y design | 38 | 33,392 | **+17.28pt** | 86.84% | pass |
| 1y confirm | 18 | 18,655 | **+19.36pt** | 77.78% | pass |
| 3y design | 21 | 18,179 | **+77.72pt** | 100% | pass |
| 3y confirm | 19 | 16,394 | **+88.67pt** | 100% | pass |

### control

値はcontrol strata内の良い半分−悪い半分。全control・全窓でmedian正、trap非悪化だった。

| control | 1y design median / trap | 1y confirm | 3y design | 3y confirm |
| --- | ---: | ---: | ---: | ---: |
| current trailing PER | +5.57pt / −9.74pt | +4.11pt / −6.98pt | +20.76pt / −15.83pt | +18.44pt / −12.05pt |
| PBR | +5.44pt / −7.05pt | +3.87pt / −4.05pt | +15.62pt / −12.38pt | +23.69pt / −11.57pt |
| market cap | +7.99pt / −14.15pt | +13.75pt / −16.56pt | +28.49pt / −24.99pt | +49.07pt / −30.48pt |
| sector 33 | +5.73pt / −10.20pt | +8.86pt / −11.80pt | +22.85pt / −18.19pt | +32.70pt / −21.64pt |

副診断の5 FY正規化PERも、算出できたcohortでは4窓すべて正方向だった。1y design / confirm spreadは+21.63pt / +22.22pt、3y design / confirmは+84.39pt / +99.14pt。ただしdesign側coverageが低いため3 FYの代替採用理由にはしない。

H-1は事前登録条件をすべて満たすため`adoption_candidate`とする。productionへの接続方法は、正規化PER warningとanchor分母のどちらを検証するかを先に固定し、3y/5y authorityを通す別issueで判断する。

## 4. H-2: cycle peak flag

E[r]上位decile内でunflagged−flaggedを比較した。

| window | comparable cohort | eligible n | flagged / unflagged | median delta | positive share | trap delta | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1y design | 37 / 38 | 3,680 | 994 / 2,686 | +3.25pt | 59.46% | **+1.13pt** | fail |
| 1y confirm | 18 / 18 | 2,109 | 663 / 1,446 | +1.85pt | 72.22% | **+0.57pt** | fail |
| 3y design | 20 / 21 | 1,908 | 316 / 1,592 | +11.39pt | 60.00% | +0.49pt | fail |
| 3y confirm | 19 / 19 | 1,953 | 746 / 1,207 | **−12.69pt** | 26.32% | +5.48pt | fail |

1y designは効果量下限を超えたが、positive shareが60%へ届かずtrapも悪化した。confirmと3y confirmが反証したため、閾値0.8を動かさず`negative`とする。H-1の連続軸が通ったことを理由にH-2の二値flagを採らない。

## 5. H-3: self-range variant

### 1250 vs production 750

deltaは1250−baseline。calibration errorは小さいほど良いため、表では`error improvement = baseline−1250`とする。

| window | error improvement | realized spread delta | top-10 median delta | top-10 trap delta | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1y design | **−0.25pt** | +1.05pt | −0.54pt | −2.41pt | error fail |
| 1y confirm | +0.27pt | **−2.64pt** | **−3.63pt** | **+7.77pt** | fail |
| 3y design | +0.76pt | +2.25pt | +2.00pt | −5.56pt | direction pass |
| 3y confirm | +0.43pt | **−0.68pt** | +4.83pt | **+0.22pt** | fail |

全cohortでtop-10の順序が変わり、baselineとの平均ticker overlapは1y design 6.92、confirm 7.28、3y design 6.78、confirm 7.56だった。影響は十分あったが、confirmで候補品質を悪化させたため`negative`とする。

### 2500 partial-history診断

2500はfull-cap coverage 0なので採用不可。partial-historyでも、1y confirmはerror improvement +0.13ptに対しspread −0.93pt、top-10 median −10.23pt、trap +15.00ptだった。良い3y designだけを採らない。

## 6. 独立検算

`2024-02-29`の1y cohortをpanel / forward CSVから別計算した。

- population median return: −4.5714%
- `normalized_per_3fy`: n=1,113、良いdecile−悪いdecile median spread **+16.8810pt**
- cycle flag: flagged 35 / unflagged 91、median delta +9.4662pt、trap delta −3.0769pt
- production top-10: median excess +15.0888%、trap 2/10
- 1250 top-10: median +12.7526%、trap 3/10、productionとのticker overlap 6/10
- 2500 top-10: median +7.3935%、trap 3/10

同cohortの`6877`は最新連続FY EPSが2021-09期365.69円、2022-09期393.20円、2023-09期449.27円、as-of close 3,785円で、`3785 / mean(365.69, 393.20, 449.27) = 9.3985896`。panelの`normalized_per_3fy`と一致した。対象期間にsplit eventは無かった。

単一cohortでcycle flagが良好でも、固定4窓のnegative判定を上書きしない。

## 7. 運用上の意味と限界

- production self-rangeは750 sessionsのまま、E[r] model version、FV、selection rules、candidate JSON、UIを変更しない。
- 正規化PERはestimate由来の軸であり、正常利益や企業価値の事実ではない。
- FY平均は業種別cycle長、利益率、commodity価格、構造変化をモデル化しない。H-1の強い相関を因果や最適分母と読まない。
- 3y forward窓は重なる。cohort数を独立標本数として扱わず、有意性・track recordを主張しない。
- 2500は保存履歴そのものが不足する。partial-history結果を2500-session evidenceと呼ばない。
