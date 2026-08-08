---
title: "信用需給 gate・空売り残・規模内混雑の計測結果"
summary: "制度信用期日偏重gateはtrapを改善せず不採用、空売り残/ADVは追加長期検証候補、規模帯内の信用買い残は不採用と判定した。"
doc_type: report
status: active
date: 2026-08-02
---

# 信用需給 gate・空売り残・規模内混雑の計測結果

価値tier: T1 — 建玉 overhang と informed short の候補を selection 前に識別し、一次リサーチ枠へ入るバリュートラップを減らす。

## 結論

- **H-1 `margin_std_long_share >= 0.75` gate は `negative`。現行rulesを維持する。** 1y top-10のvariant−baselineはdesignでmedian −0.10pt / trap +1.08pt、非重複confirmで−0.14pt / +1.17ptだった。gateは両窓で実際に順位を変えたが、trapを減らさなかった。
- **H-2 `margin_short_to_adv` は `adoption_candidate`。** 1y decile spreadはdesign +7.28pt、confirm +15.48pt、authority eligibleな3y design +9.85ptで、positive cohort shareは94.74% / 94.44% / 100%。1y両窓の全controlもmedian正・trap非正だった。ただし本issueではwarning、gate、ranking、E[r]へ接続しない。
- **H-3 `margin_long_to_adv_mcap_quintile_percentile` は `negative`。** 1y designは+0.79ptと効果量未達、confirmは−5.45pt、authority eligibleな3y designも−7.36ptで仮説と逆だった。
- **判断面の残作業は完了。** `/stocks/shortlist` の比較表に4軸の信用需給列、候補cardに4つの個別行を表示し、既存 `制度期日偏重` flagも同じrowのdata-quality表示へ届く。

H-1の不採用は、`margin_std_long_share` と将来returnの関連を否定するものではない。元のaxis評価は正だったが、固定閾値でrecommendedから除外して補充する具体的なpolicyがresearch枠のtrapを改善しなかった、というpolicy-levelの反証である。

## 1. 事前登録と実行契約

入力境界、列、方向、時間窓、control、採否条件は、forward outcome計測前のcommit `5673a56` と [`2026-08-02-margin-supply-demand-adoption-preregistration.md`](./2026-08-02-margin-supply-demand-adoption-preregistration.md) で固定した。計測実装とUI配線は outcome計測前のcommit `1f6b455` で固定し、authority sensitivityを同じ採否条件へ揃える修正はcommit `ad5c266` で固定してから全artifactを再生成した。

実行コマンド:

```bash
.venv/bin/baibai-engine screening calibration-build \
  --start 2019-11-01 --end 2026-07-31 --force
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 1y --start 2019-11-29 --end 2022-12-30 \
  --out /tmp/margin-supply-demand-1y-design.yaml
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 1y --start 2024-01-31 --end 2025-06-30 \
  --out /tmp/margin-supply-demand-1y-confirm.yaml
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 3y --start 2019-11-29 --end 2020-05-29 \
  --out /tmp/margin-supply-demand-3y-design.yaml
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 3y --horizon 5y --run-purpose production_decision \
  --required-asof 2020-01-31 --required-asof 2020-05-29 \
  --required-asof 2021-01-29 --required-asof 2021-05-31 \
  --required-metric recommended_rank_top5 \
  --required-metric recommended_rank_top10 \
  --required-metric er_calibration \
  --required-metric margin_deadline_gate_top10 \
  --out /tmp/margin-supply-demand-long-authority.yaml
```

H-1 が `negative` で確定したため、`margin_deadline_gate_top10` の評価枝と authority 登録は通常 tree に無い。上の authority run を再実行するには、その退役より前の commit を checkout する。

- cache schema: `8`
- panel: 80 cohort
- forward: 1,509,220行、resolved 1,055,260行。schema v7時点と一致
- 1y design SHA-256: `298dbc06794ba5c818e577cb576c60bf13fc04cd5caa8ff3ec95f59e6932e45c`
- 1y confirm SHA-256: `72fe83294c62ab759d32a20af573ba9326c9cc8720b6f70fe7a46fc95c9180b5`
- 3y design SHA-256: `450a03a509548f5b47b73254fac8324bf51b8b89fe8e39cea53b19dc638ae528`
- long authority SHA-256: `a340a4e0343531a73e194ae0ce49d68f8ab00ecf4e6b62b496cb1ec57596f6dd`

metricはcohortのresolved流動性母集団中央値に対する `price_return_only` excess、trapは `excess < -0.20` である。

## 2. availability

80 panelの `in_population` 107,802行について、欠損を0へ補完していない。

| field | non-null | coverage | 限定 |
| --- | ---: | ---: | --- |
| `margin_short_to_adv` | 94,496 | 87.66% | 貸借銘柄だけ。信用銘柄の構造的short 0はnull |
| `margin_long_to_adv_mcap_quintile_percentile` | 106,568 | 98.86% | 時価総額と元のlong/ADVがある流動性母集団だけ |
| `realized_volatility_60d` | 107,227 | 99.47% | 60日control。候補表示・rankingには使わない |

`margin_short_to_adv` は `short / (long + short)` ではないため、既観測 `margin_long_share` の単なる補数ではない。ADV窓内の分割、ADV欠損、貸借対象外はnullのまま落ちる。

## 3. H-1: 制度信用期日偏重gate

### 1y leading判定

| window | paired cohort | baseline / variant n | changed share | top-10 median delta | top-10 trap delta | top-5 median / trap delta | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| design | 38 | 375 / 377 | 94.74% | **−0.10pt** | **+1.08pt** | +1.30pt / +1.05pt | fail |
| confirm | 18 | 175 / 175 | 50.00% | **−0.14pt** | **+1.17pt** | +0.36pt / −1.11pt | fail |

影響条件は両窓で満たしたが、top-10のtrapが両方で悪化した。事前登録した「非劣後・trap改善」と「excess改善・trap非悪化」のどちらにも入らない。top-5のconfirmだけを採ってtop-10主判定を上書きしない。

### 固定4 as-ofの長期確認

leading failのためproduction採用へ進む条件は既に失っているが、事前登録した `production_decision` runも実行した。

| horizon | cohort | baseline / variant n | changed share | top-10 median delta | top-10 trap delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| 3y | 4 | 38 / 39 | 100% | **−5.31pt** | 0.00pt |
| 5y | 4 | 38 / 37 | 100% | +25.52pt | −5.00pt |

3yは非劣後下限−1ptを外した。5yは良いが、`2020-05-29` の `margin_deadline_gate_top10` がdelisting全損/中立代入で採用条件（median非劣後かつtrap非悪化）を安定して満たさず、authorityは `unpriced_exit_flips:margin_deadline_gate_top10` でblockされた。良い5yだけを採らず、**H-1は `negative`、rules revisionなし**とする。

## 4. H-2: 空売り残 / ADV

### axis主判定

3y designは7 requestedのうち、既存authority blockerを抜けた `2019-12-30`、`2020-01-31`、`2020-04-30`、`2020-05-29` の4 cohortだけを集計した。

| window | cohort | n | mean decile spread | positive share | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1y design | 38 | 40,742 | **+7.28pt** | 94.74% | pass |
| 1y confirm | 18 | 21,875 | **+15.48pt** | 94.44% | pass |
| 3y design eligible | 4 | 3,965 | **+9.85pt** | 100% | pass |

高いshort/ADVほど悪い、すなわち低い側をbestとする事前登録方向で3窓が一致した。1y両窓の標本条件も満たした。

### 1y control

値は各cohortのcontrol strata内で、良い側−悪い側を `min(best_n, worst_n)` で加重した値の窓平均。全controlでmedian正・trap非正を満たした。

| control | design median / trap | confirm median / trap |
| --- | ---: | ---: |
| market cap | +2.58pt / −3.75pt | +7.63pt / −9.23pt |
| turnover | +2.73pt / −5.32pt | +7.18pt / −10.07pt |
| trailing PER | +3.23pt / −4.20pt | +3.84pt / −5.38pt |
| dividend yield | +2.41pt / −2.45pt | +4.07pt / −4.39pt |
| price level | +3.57pt / −5.41pt | +7.20pt / −8.19pt |
| 60d momentum | +3.13pt / −4.98pt | +6.80pt / −7.71pt |
| 60d realized vol | +2.61pt / −3.22pt | +8.47pt / −7.27pt |
| sector 33 | +2.90pt / −4.62pt | +4.84pt / −6.71pt |

3y eligible 4 cohortのcontrolも診断として全8本でmedian正・trap非正だった。最小medianは60d momentumの+0.91pt、最小trap改善はdividend yieldの−2.96ptである。

H-2は事前登録条件を満たすため `adoption_candidate` とする。これは空売り主体が企業価値悪化を知っているという因果の証明ではなく、次の長期production検証へ進める許可である。

## 5. H-3: 同規模帯内の信用買い残

| window | cohort | n | mean decile spread | positive share | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1y design | 38 | 46,568 | +0.79pt | 60.53% | 効果量未達 |
| 1y confirm | 18 | 24,177 | **−5.45pt** | 22.22% | 逆方向 |
| 3y design eligible | 4 | 4,615 | **−7.36pt** | 0% | 逆方向 |

confirmでは7 controlすべてのmedianが負で、realized volatility controlのtrapも+0.93ptと悪化した。規模帯内順位へ変換しても、信用買い残の厚さを独立した悪材料として使える証拠は得られなかった。H-3は `negative` とし、panel列は反証証跡として残すが判断面へ昇格させない。

## 6. 独立検算

`2024-02-29` の1y cohortをpanel / forward CSVから別計算した。流動性母集団median returnは−4.5714%。baseline top-10は `5707, 8595, 4506, 7276, 3291, 2121, 2432, 4023, 4848, 2491`、gate variantは `7276` を除き `4044` を補充した。

- baseline: n=10、median excess +15.0888%、trap 2/10
- variant: n=10、median excess +19.5934%、trap 2/10
- delta: median +4.5046pt、trap 0pt

evaluation YAMLと丸め前まで一致した。別の `2024-01-31` cohortでは、raw CSVからH-2 n=1,224・spread +9.9924pt、H-3 n=1,370・spread +8.0674ptを再計算し、cohort出力と一致した。単一cohortの良いH-3を全窓のnegative判定より優先しない。

fixtureでは、貸借/信用の区別、short正・0、ADV欠損、分割窓、時価総額quintile境界、tie平均順位、母集団外・欠損、gate閾値一致と欠損通過、除外後の順位補充、cache不正値を検算した。

## 7. UI と運用上の意味

shortlist比較表の `信用需給` 列は、買残/ADV、買残比率、26w買残変化、制度信用買残比率を単位付きでまとめる。候補cardは4軸を別行にし、nullを `—` とする。`制度期日偏重` は既存どおり `margin_std_long_share >= 0.75` で文脈flagを出すが、**recommendationから除外するgateではない**。

H-2/H-3の新列はcalibration panel専用で、candidate JSON・UI・warningへ出していない。計測候補を判断事実のように見せないためである。

## 8. この結果が意味しないこと

- H-1 negativeは、制度信用買い残を無視してよいという意味ではない。固定0.75のhard exclusionがresearch枠を改善しなかったという限定された結論である。
- H-2は借株コスト、貸株料、空売り主体、取引理由を観測しない。short/ADVとprice-only excessの関連であり、informed shortの因果推定ではない。
- 3y designは4 cohort、固定長期authorityは4 as-ofで、月次forward窓も重なる。cohort数を独立標本数として読まない。
- H-2 `adoption_candidate` はproduction変更許可ではない。独立3y confirm、5y、delisting感度と判断面の最小surfaceを別の事前登録で固定する。
- UIの4軸は観測文脈であり、E[r]、FV、売買timing、sizingを自動変更しない。
