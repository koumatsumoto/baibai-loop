---
title: "質・トラップ複合軸の計測結果"
summary: "F-score 型充足本数は E[r] 上位帯の 1y excess を改善せず、production 参入を棄却した。"
doc_type: report
status: active
date: 2026-08-02
---

# 質・トラップ複合軸の計測結果

価値tier: T1 — E[r] 上位帯のバリュートラップ率を下げ、深掘り research に入る候補の打率を上げる。

## 結論

**判定は `negative`。quality composite を gate / ranking / E[r] / candidate annotation へ参入させない。**

1y の標本条件は満たしたが、high quality（5本以上）− low quality（3本以下）の median excess は design −5.18pt、confirm −9.71pt で、事前登録した +3pt の下限と逆方向だった。design の trap rate も +3.94pt 悪化した。3y design は median excess −28.81pt、trap rate +0.54ptで、同じく仮説を支持しない。3y confirm は authority blocker により `insufficient` だが、1y の再現した逆方向だけで採用条件は成立しない。

個別成分と composite は再現可能な計測証跡として calibration panel に残す。production candidate metrics には出さず、research 表示へも格下げしない。#722 が個別成分を条件候補として参照する場合も、本 composite の予測力を前提にしない。採用 issue は起票しない。

## 1. 事前登録と実行契約

成分、field 名、方向、時間窓、交絡順序、採否条件は結果計測前の commit `ffa2641d20d8a53035315a2c43f83ae95030657f` と [`2026-08-02-quality-trap-composite-preregistration.md`](./2026-08-02-quality-trap-composite-preregistration.md) で固定した。実装は計測前の commit `19f5a351f859a6b82b1c7141e2081c828fa7fa50` で固定した。

実行コマンド:

```bash
.venv/bin/baibai-engine screening calibration-build \
  --start 2019-11-01 --end 2026-07-31 --force
.venv/bin/baibai-engine screening calibration-evaluate \
  --horizon 1y --horizon 3y --out /tmp/quality-trap-eval.yaml
```

- cache schema: `6`
- panel: 80 cohort
- forward rows: 1,509,220（resolved 1,055,260）
- evaluation SHA-256: `8d74176e1b2f6649d2c47980e4d4d85fbeaa4e7755bf2f92ed95f0bb416db944`
- metric: cohort の resolved 流動性母集団中央値に対する `price_return_only` excess
- trap: `excess < -0.20`

## 2. 成分 availability

80 panel の `in_population` 107,802 row のうち、6成分以上を観測できて composite が有効なのは27,454 row（25.47%）だった。欠損は不充足へ補完していない。

| available count | rows |
| ---: | ---: |
| 0 | 4,549 |
| 1 | 273 |
| 2 | 48,702 |
| 3 | 370 |
| 4 | 26,345 |
| 5 | 109 |
| 6 | 1,128 |
| 7 | 1,432 |
| 8 | 24,894 |

| component | non-null rows | coverage |
| --- | ---: | ---: |
| ROA 正 | 27,563 | 25.57% |
| ΔROA 正 | 27,563 | 25.57% |
| CFO 正 | 53,968 | 50.06% |
| accruals 健全 | 53,962 | 50.06% |
| Δ営業 margin 正 | 25,996 | 24.11% |
| Δequity ratio 正 | 102,393 | 94.98% |
| 希薄化なし | 101,712 | 94.35% |
| Δasset turnover 正 | 27,499 | 25.51% |

TTM delta に必要な current/prior の同一 flow basis が主な制約である。availability が低くても threshold を緩めたり、異なる利益概念を比較したりはしていない。

## 3. 主検定

authority/coverage は現行 calibration 判定を適用した。1y は leading evidence として resolved cohort を使い、3y は master、survivorship、adjustment factor、input range、candidate partition、universe 未評価、entry gap、delisting sensitivity を満たす cohort だけを集計した。

| window | requested / integrity eligible | delta cohorts | high n / low n | mean median excess delta | positive cohort share | mean trap delta | mean axis decile spread | 判定 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1y design | 38 / 38 | 27 | 679 / 209 | **−5.18pt** | 37.04% | **+3.94pt** | −1.86pt | negative |
| 1y confirm | 18 / 18 | 16 | 546 / 85 | **−9.71pt** | 43.75% | −9.18pt | +10.75pt | negative |
| 3y design | 7 / 4 | 3 | 57 / 25 | **−28.81pt** | 33.33% | **+0.54pt** | −21.94pt | negative |
| 3y confirm | 1 / 0 | 0 | 0 / 0 | — | — | — | — | insufficient |

1y は両窓とも high/low 各100件以上、delta cohort 6以上を満たす。3y design は各群15件以上、delta cohort 3を満たす。3y confirm の `2023-06-30` は `priced_master_without_universe_return_unresolved` で authority 対象外だった。3y design の除外理由は `entry_price_gap`、`priced_master_without_universe_flips_direction`、`priced_master_without_universe_return_unresolved`、`unpriced_exit_flips_direction` 各1件で、一部 cohort は複数理由を持つ。

confirm の quality-count axis decile spread と trap delta だけは仮説方向だが、主統計量の median excess delta が −9.71ptであり、単独の良い列へ採否を切り替えない。

## 4. 交絡確認

途中で不支持になっても止めず、事前登録順に6 control をすべて適用した。値は各 cohort 内二分位 strata の high−low を `min(high_n, low_n)` で重み付けし、その cohort 値を窓内で単純平均したもの。

| control | design cohorts / weight | design median / trap delta | confirm cohorts / weight | confirm median / trap delta |
| --- | ---: | ---: | ---: | ---: |
| `market_cap_oku` | 10 / 144 | −4.15pt / +0.98pt | 5 / 60 | +6.59pt / −2.66pt |
| `avg_turnover_oku` | 11 / 144 | −5.59pt / −1.44pt | 4 / 55 | +3.48pt / −1.36pt |
| `pbr` | 9 / 128 | +0.18pt / +1.80pt | 3 / 50 | +7.87pt / −0.79pt |
| `per_trailing` | 6 / 56 | −0.49pt / +2.89pt | 3 / 19 | +14.34pt / +12.85pt |
| `dividend_yield` | 9 / 130 | −1.70pt / +1.56pt | 3 / 45 | +7.58pt / −2.55pt |
| `price_change_60d` | 9 / 128 | −6.24pt / +3.63pt | 3 / 43 | +5.33pt / +1.25pt |

design は6 control中4つで median delta が負、5つで trap delta が正だった。confirm も PER と momentum の trap delta が正で、全 control が両窓で仮説方向という採否条件を満たさない。control ごとの有効 cohort は3〜11と疎であり、良い方向の一部 control だけを因果効果とはみなさない。

## 5. 検算

- fixture で8成分すべて true、current/prior の利益 fallback 不一致時の ROA 欠損、available 5の composite null、available 6で支持0本、CSV boolean/count 改ざんの fail-closed を確認した。
- `2024-01-31` 1y cohort を panel/forward CSV から独立再計算した。population median return −0.4143%、E[r] top decile 139、composite eligible 17、high 8 / low 4、high median excess −4.6358%、low +23.5138%、delta −28.1496%で evaluation YAML と一致した。trap 件数は両群0だった。
- panel cache round-trip、schema v5拒否、全80 panel の v6 header、rebuild 件数を確認した。

## 6. 判定表

| 事前登録条件 | 観測 | 判定 |
| --- | --- | --- |
| 1y / 3y axis decile spread が正 | 1y design −1.86pt、3y design −21.94pt | fail |
| 1y design / confirm median delta ≥ +3pt | −5.18pt / −9.71pt | fail |
| 1y design / confirm trap delta < 0 | +3.94pt / −9.18pt | fail |
| 3y design / confirm median > 0、trap < 0 | design は −28.81pt / +0.54pt、confirm は authority 不足 | fail / insufficient |
| 1yの全6 controlで median > 0、trap ≤ 0 | design・confirm とも複数不通過 | fail |
| 標本・authority | 1yと3y designは標本充足、3y confirmは blocker | partial |

総合判定は `negative`。3y confirm の不足は残るが、十分な1y標本で主効果が両窓とも逆方向なので、追加満期だけで採用候補へ戻さない。

## 7. 監視事項

- quality component は panel 診断に限る。候補表示、rank、gate、E[r]、research checklistへ接続しない。
- #722 では「急落×質」を別の事前登録仮説として検定できるが、本 report の count 閾値や符号を有効と仮定しない。
- TTM delta の coverage が将来大きく改善しても、本検定の閾値を変更して同じ仮説を救済しない。再検定するなら新しいデータ契約と独立した事前登録を要求する。
- 3y confirm blocker が解消しても、1y negative を覆す production 参入判断には使わない。長期診断の補足としてのみ更新する。
