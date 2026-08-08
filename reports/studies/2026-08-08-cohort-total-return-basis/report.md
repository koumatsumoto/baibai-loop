---
title: "cohort 比較の total-return basis — carry の効果量は price-only で過小に出ていた"
summary: "cohort 比較を実現配当込みの basis で測り直した。全 4 軸で結論の向きは変わらず、carry 軸の差は 3y で +7.65pt から +8.99pt へ広がった。母集団の実現年率も 3y で 5.88% から 7.89% へ上がり、price-only は群差だけでなく水準そのものを約 2pt 過小に出していた。"
doc_type: measurement-record
status: active
---

# cohort 比較の total-return basis

価値tier: T1 — carry は較正で最も強い軸で、その効果量を測る basis が実際に払われる現金を含んでいなかった。

対象 issue: [#838](https://github.com/koumatsumoto/baibai-loop/issues/838)。仮説は「price-only は carry 群の差を過小に出す」で、**実データはこれを支持する**。

## 1. 再現手順

```bash
uv run python -m tools.experiments.measure_signal_cohorts --basis price --out .cache/838-price.yaml
uv run python -m tools.experiments.measure_signal_cohorts --basis total --out .cache/838-total.yaml
```

panel は 80 本（2019-11-29..2026-06-30）、`rules_hash` は `ec8c87c50da68cff`。

## 2. 2 つの basis は同じ行を覆わない

`total` は窓内に FY 配当の観測を要するので、`price` より母数が小さい。比は horizon で変わる。

| horizon | price 解決行 | total 解決行 | 比 | 並べて読めるか |
| --- | ---: | ---: | ---: | --- |
| 3m | 293,047 | 60,436 | 0.206 | ✗ |
| 6m | 280,196 | 137,833 | 0.492 | ✗ |
| 1y | 254,817 | 242,996 | 0.954 | ✓ |
| 3y | 157,874 | 146,263 | 0.926 | ✓ |
| 5y | 69,326 | 61,087 | 0.881 | ✓ |

**3m / 6m では total basis の中央値を price と並べない。** 窓が短いほど FY 配当の観測に届かず、残った行は「窓内に決算期末を含んだ銘柄」へ偏る。tool は `basis_coverage.bases_comparable` でこれを表明する（閾値 0.75）。

以下は 1y / 3y / 5y だけを扱う。

## 3. 4 軸すべてで向きは変わらず、差は広がった

年率中央値（%）。`n` は解決行数。

### 3.1 機械 E[r] ≥ 8.5%

| horizon | | price | total |
| --- | --- | ---: | ---: |
| 1y | treatment | 21.64 (n=1,582) | **25.60** (n=1,527) |
| | 母集団 | 5.62 (n=94,320) | **8.18** (n=89,496) |
| | cohort 一致 | 39/48（+10.81pt） | **35/44（+11.89pt）** |
| 3y | treatment | 16.48 (n=1,265) | **19.89** (n=1,200) |
| | 母集団 | 5.88 (n=57,821) | **7.89** (n=52,764) |
| | cohort 一致 | 31/33（+9.95pt） | **30/31（+11.08pt）** |
| 5y | treatment | 14.66 (n=961) | **16.83** (n=856) |
| | 母集団 | 5.95 (n=26,198) | **7.65** (n=22,460) |
| | cohort 一致 | 20/20（+8.41pt） | **19/19（+8.45pt）** |

### 3.2 buyback carry が clip に貼り付いた群

| horizon | 群 | price | total |
| --- | --- | ---: | ---: |
| 1y | clip | 12.30 (n=3,050) | 15.82 (n=2,871) |
| | 非正 | 4.67 (n=79,479) | 7.14 (n=75,564) |
| | cohort 一致 | 56/69（+6.08pt） | **57/68（+7.31pt）** |
| 3y | clip | 13.80 (n=1,368) | **17.05** (n=1,298) |
| | 非正 | 5.31 (n=49,846) | 7.29 (n=45,506) |
| | cohort 一致 | 43/45（+7.65pt） | **43/45（+8.99pt）** |
| 5y | clip | 11.93 (n=443) | 13.35 (n=360) |
| | 非正 | 5.62 (n=22,822) | 7.27 (n=19,547) |
| | cohort 一致 | 21/21（+5.97pt） | 19/21（+6.29pt） |

**仮説どおり差は広がった。** 3y で +7.65pt → +8.99pt、1y で +6.08pt → +7.31pt。clip 群は配当も多い側なので、配当を落とした basis はその分だけ差を削っていた。

ただし 5y の cohort 一致は 21/21 → 19/21 へ落ちる。clip 群の解決行が 443 → 360 に減り、配当を観測できなかった月が抜けたためで、差の大きさ（+5.97 → +6.29pt）とは逆向きに動く。**「全 cohort 一致」を basis 横断の見出し数値にしない。**

### 3.3 株数減少の継続性

| horizon | 群 | price | total |
| --- | --- | ---: | ---: |
| 3y | 単年 | 12.06 (n=1,932) | 14.71 (n=1,784) |
| | 反復 | 13.35 (n=1,078) | 15.88 (n=1,012) |
| | cohort 一致 | 27/45（+2.40pt） | **24/43（+1.43pt）** |
| 5y | 単年 | 10.00 (n=631) | 10.92 (n=542) |
| | 反復 | 13.19 (n=543) | 16.19 (n=488) |
| | cohort 一致 | 17/21（+4.06pt） | **17/21（+5.44pt）** |

3y では反復群の優位が total basis で**縮む**（+2.40 → +1.43pt、一致も 27/45 → 24/43）。この軸だけは basis を変えると弱くなる方向で、もともと cohort 一致が半数付近なので、どちらの basis でも継続性を根拠に使える強さではない。

### 3.4 PBR 単独 anchor で reversion が cap に当たった群

| horizon | 群 | price | total |
| --- | --- | ---: | ---: |
| 3y | cap 到達 | 15.04 (n=615) | 16.60 (n=583) |
| | cap 未満 | 1.72 (n=5,479) | 3.31 (n=5,002) |
| | cohort 一致 | 16/17（+12.94pt） | **14/14（+13.08pt）** |

## 4. 見落としやすい所見: 水準そのものが約 2pt 過小だった

群差だけでなく**母集団の実現年率が上がる**。3y で 5.88% → 7.89%、5y で 5.95% → 7.65%。

E[r] の絶対水準較正（`er_level_calibration`）は既に total-return 座標で行っているので、そちらとの整合はむしろ改善する。price-only の母集団中央値を「日本株の実現水準」として引用していた箇所があれば、それは配当を落とした値である。

## 5. 判定

```yaml
carry_effect_understated_by_price_only: supported
conclusion_direction_changes_with_basis: not_found
buyback_continuity_weakens_on_total_basis: observed
population_level_understated_by_price_only: supported_about_2pt
```

#833 の診断（[2026-08-06](../2026-08-06-bargain-capture-diagnosis/report.md)）が §9 で予測した「price-only は差を過小に出す方向であり、結論の向きは変わらない」は、4 軸中 3 軸で確認された。残る 1 軸（3.3 株数減少の継続性）は差が縮むが、もともと cohort 一致が半数付近で結論を担っていない。

## 6. 誠実性の限定

- forward 窓は重なり独立でない。有意性・track record を主張しない。
- total basis は窓内の実現 FY 配当だけを足し、端 FY の月割りをしない。中間・期末配当の権利落ち日を再現する cash-flow ledger ではない。
- 2 つの basis は母数が違う。同じ群を別の物差しで測ったのではなく、**部分集合を別の物差しで測っている**。3.2 の 5y のように、差が広がりながら cohort 一致が落ちる組み合わせが起きる。
- 実現値は 2023〜2026 の日本株上昇局面を含む。regime 統制された量は同一 as-of の母集団との差だけである。
- 廃止で系列が切れた銘柄は resolved に入らない。
- 母集団水準の上昇（§4）は配当込みへ変えた効果であって、企業の還元が増えた観測ではない。

## 7. 監視事項

- panel が増えるたびに両 basis で再計算する。特に 3.3 は basis 間で向きが割れているので、cohort 一致が片側で 60% を超えるまで結論に使わない。
- 3m / 6m の total 被覆は窓の短さに由来する構造的なもので、panel を増やしても改善しない。`bases_comparable` が true にならない限りこの 2 horizon は price のみで読む。
