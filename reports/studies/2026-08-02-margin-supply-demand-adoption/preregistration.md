---
title: "信用需給 gate・空売り残・規模内混雑の事前登録"
summary: "制度信用期日偏重の selection gate、空売り残の厚さ、同規模帯内の信用買い残を固定条件で検証する。"
doc_type: report
status: active
date: 2026-08-02
---

# 信用需給 gate・空売り残・規模内混雑の事前登録

価値tier: T1 — 建玉 overhang と informed short の候補を selection 前に識別し、一次リサーチ枠へ入るバリュートラップを減らす。

本書は #720 の forward outcome を新たに計測する前に、H-1〜H-3 の列、方向、母集団、時間窓、交絡確認、採否条件を固定する。数値を見た後に閾値、方向、窓、統計量を変更しない。方向または効果量の未達は `negative`、標本または authority の未達は `insufficient` とする。

## 1. 既知情報と独立性

`reports/2026-07-31-margin-supply-demand-preregistration.md` で次を観測済みである。

- `margin_std_long_share` は 1y / 3y と時間分割で仮説方向に出た。判断面の既存 flag は、当時の候補・流動性母集団の第9 decile 境界に対応する固定値 `0.75` を使う。
- `margin_long_share` は事前登録方向と逆に強く出た。したがって同列の方向だけを反転して再評価しない。
- `margin_long_to_adv` は時価総額との順位相関が強く、時価総額 quintile 内で比較すると 1y の効果がほぼ消えた。

H-1 は上記の採用候補を selection へ接続する検定であり、元軸の再検定ではない。H-2 は `margin_long_share` の厳密な補数となる `short / (long + short)` を採らず、新しい量 `short / ADV` に固定する。H-3 は生の `margin_long_to_adv` ではなく、同規模帯内の順位を新しい列として固定する。

## 2. 固定する列と方向

| field | 型・定義 | 仮説方向 |
| --- | --- | --- |
| `margin_short_to_adv` | `float | None`。最新公表週の `ShrtVol / 20取引日平均出来高株数`。貸借銘柄 (`IssueType == "2"`) だけを対象にし、ADV窓内に分割・併合があれば null | 高いほど悪い (`direction=-1`) |
| `margin_long_to_adv_mcap_quintile_percentile` | `float | None`。各 cohort の `in_population` かつ時価総額・`margin_long_to_adv` 非欠損行を時価総額 quintile へ分け、その帯内で `margin_long_to_adv` の tie-aware percentile rank を `[0, 1]` で表す | 高いほど悪い (`direction=-1`) |
| `realized_volatility_60d` | `float | None`。as-of までの調整後終値60取引日の日次リターン標準偏差を年率化 (`sqrt(252)`)。交絡確認専用 | 採否方向なし |

`margin_short_to_adv` は信用銘柄の構造的な short 0 を観測値として扱わず null にする。short 0 は貸借銘柄では有効な0である。ADVが0・欠損、または窓内に分割・併合があるときは null とし、補完しない。

時価総額 quintile は `(market_cap_oku, ticker)` の安定順で、`int(index * 5 / n)` の5群へ割り当てる。各帯内 percentile は平均順位を用い、帯が1行なら `0.5` とする。この列は元の `margin_long_to_adv` を規模帯の内側で順序化するだけで、時価総額によるリターン補正は行わない。

## 3. H-1: selection gate variant

固定 gate は次のとおり。

```text
exclude_from_recommendation = margin_std_long_share is not null
                              and margin_std_long_share >= 0.75
```

欠損は「最悪ではない」とは判断せず、現行どおり通過させる。candidates、screen pass、E[r]、full ranking は変えない。各 cohort の production-compatible `recommended_rank` 順を baseline とし、gate で除外した後に順位を詰め直した列を `margin_deadline_gate_rank` とする。保存深度50の範囲で top-20 まで補充できない cohort は当該 top-N を unresolved とする。

各 cohort の top-5 / top-10 について、既存と同じ流動性母集団中央値に対する `price_return_only` excess と `excess < -0.20` の trap を集計する。variant − baseline の列は次に固定する。

- `margin_deadline_gate.top5.{baseline,variant,median_excess_delta,trap_rate_delta}`
- `margin_deadline_gate.top10.{baseline,variant,median_excess_delta,trap_rate_delta}`
- `margin_deadline_gate.{excluded_count,top5_changed,top10_changed}`

cohort 横断では、両側に1件以上の resolved return がある paired cohort の delta を単純平均し、changed cohort share、baseline / variant の合計 n、除外件数を報告する。有意性検定や、重なる月次 cohort を独立標本とみなす推測統計は使わない。

## 4. H-2 / H-3: axis と交絡確認

H-2 / H-3 は既存 generic axis と同じく、各 cohort で軸を方向倍して昇順に並べ、decile 10を良い側、decile 1を悪い側とする。主統計量は `decile_spread_median = best - worst`、best decile trap rate、rank IC、eligible n とする。

交絡は次の順にすべて確認する。数値 control は cohort 内 quintile、sector は `sector_33` で層別し、各 stratum 内で軸の良い側 / 悪い側を半分に分ける。両側各5行以上の stratumだけを使い、stratum の重み `min(best_n, worst_n)` による median excess spread と trap rate delta の加重平均を出す。control同士を交差させない。

| order | control | 対象 |
| ---: | --- | --- |
| 1 | `market_cap_oku` | H-2。H-3はこの層別を列定義に含むため重複適用しない |
| 2 | `avg_turnover_oku` | H-2 / H-3 |
| 3 | `per_trailing` | H-2 / H-3 |
| 4 | `dividend_yield` | H-2 / H-3 |
| 5 | `close` | H-2 / H-3 |
| 6 | `price_change_60d` | H-2 / H-3 |
| 7 | `realized_volatility_60d` | H-2 / H-3 |
| 8 | `sector_33` | H-2 / H-3 |

固定列は `margin_supply_demand_hypotheses.<axis>.controls.<control>.{strata_used,matched_weight,stratified_median_excess_spread,stratified_trap_rate_delta}` とする。H-3の `market_cap_oku` は `normalized_in_axis=true` と記録し、統計値を捏造しない。

## 5. 時間分割と authority

| horizon | design cohort as-of | confirm cohort as-of | 用途 |
| --- | --- | --- | --- |
| 1y | 2019-11-29〜2022-12-30 | 2024-01-31〜2025-06-30 | 主判定。design exit は2023年末まで、confirm entry は2024年以降で非重複 |
| 3y | 2019-11-29〜2020-05-29 | — | H-2 / H-3 の長期方向を確認する補助 |

H-1 を production rules へ反映できる長期確認は、既存 authority 計測で3y / 5yの双方が eligible だった4 as-ofを結果を見る前に固定する。

- `2020-01-31`
- `2020-05-29`
- `2021-01-29`
- `2021-05-31`

この4 as-of × `3y,5y` を `production_decision` の required scope とし、core metricに `margin_deadline_gate_top10` を加える。8組すべてが authority と metric status を通ることを要求する。過去の eligibility は既知だが、本変更後の metric が解決することと結論の方向は未観測である。

## 6. 採否基準

### H-1 leading pass

1y design / confirm の双方で次をすべて満たす場合だけ long-horizon判定へ進める。

1. top-10 paired cohortが6以上、baseline / variant の resolved合計が各60以上。
2. `top10_changed` cohort shareが25%以上。gateが実質的に何も変えない variant は不採用。
3. top-10 が次のどちらかを満たす。
   - 非劣後・trap改善: mean median excess delta `>= -0.01` かつ mean trap rate delta `<= -0.02`
   - excess改善・trap非悪化: mean median excess delta `>= 0.03` かつ mean trap rate delta `<= 0`
4. top-5 の重大悪化がない: mean median excess delta `>= -0.02` かつ mean trap rate delta `<= 0.02`。

### H-1 production adoption

leading passに加え、固定した4 as-ofの3y / 5y `production_decision` が全8組 eligibleであり、3y / 5yそれぞれの top-10 mean median excess delta `>= -0.01`、mean trap rate delta `<= 0` を満たす場合だけ、`selection.supply_demand.margin_std_long_share_exclude_at_or_above: 0.75` を新しい rules revisionへ反映する。1つでも方向・authorityを外せば現行rulesを維持する。反映時は panel を `--force` で再構築し、現 as-of の selection 前後差分で、candidates件数不変、除外ticker、補充ticker、順位、欠損通過を確認する。

### H-2 / H-3

各軸を `adoption_candidate` とする条件は次のすべてである。

1. 1y design / confirm の双方で mean decile spread `>= 0.03`、positive cohort share `> 0.5`。
2. 3y design で mean decile spread `> 0`、positive cohort share `> 0.5`。
3. 1y design / confirm の対象 control すべてで、mean stratified median excess spread `> 0` かつ mean stratified trap rate delta `<= 0`。
4. 各1y窓でaxis算出可能 cohortが6以上・axis合計nが各600以上、3y designでauthority eligible cohortが3以上・axis合計nが300以上。各controlは窓内で利用可能 cohortが3以上・matched weight合計100以上。

`adoption_candidate` でも H-2 / H-3 を本issueで warning、gate、ranking、E[r]へ接続しない。新しい production surface は独立した長期確認を事前登録する。negativeでもpanel列とreportは再現可能な反証証跡として残す。

## 7. UI と検算

既存4軸 `margin_long_to_adv`、`margin_long_share`、`margin_long_delta_26w`、`margin_std_long_share` と既存 `制度期日偏重` flagを `/stocks/shortlist` の比較表・候補cardへ表示する。H-2 / H-3 は本issueの未採用検定列なので表示しない。

実装では次を検算する。

- 貸借銘柄のshort正・0、信用銘柄の構造的0、ADV欠損、ADV窓内分割で `margin_short_to_adv` が定義どおりになる。
- 時価総額 quintile境界、tieの平均順位、1行stratum、母集団外・欠損行がH-3列へ混入しない。
- gate閾値の直下・一致・直上、欠損通過、除外後の順位詰めとtop-N補充をfixtureで固定する。
- panel CSV round-trip、負のshort/ADV、`[0,1]` 外percentile、依存field欠損をfail-closedにし、cache schemaとAP-08を更新する。
- 代表cohortについて、gate前後ticker、top-10 median、trap件数、H-2/H-3 decile境界を保存CSVから別計算する。
- UIは4軸のnullを `—` として表示し、比率と日数相当を混同しない。

forward returnは `price_return_only` であり、借株コスト・配当・貸株料を含まない。信用残は週次公表値で、空売り主体や取引理由を識別しない。軸の関連が成立しても、informed shortという因果を証明しない。
