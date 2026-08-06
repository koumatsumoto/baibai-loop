---
title: "お買い得捕捉経路の診断 — ゼロ購入 6 cycle の所在"
summary: "6 cycle 連続ゼロ購入の原因を漏斗の各段で計測した。市況でも候補供給でもなく、機械 top と research hurdle の系統的な乖離が主因で、上流 E[r] の 2 つの水増し仮説はどちらも実データで否定された。"
doc_type: measurement-record
status: active
last_reviewed: 2026-08-06
---

# お買い得捕捉経路の診断

価値tier: T1 — 「割安を拾えていない」の原因を漏斗の段ごとに特定し、どの層を直せば拾える確率が上がるかを実データで決める。

対象 issue: [#833](https://github.com/koumatsumoto/baibai-loop/issues/833)。#833 は診断仮説として「上流 E[r] の残像汚染」を第 1 要因に置いていたが、**本計測はその仮説を否定する**。以下は計測と、そこから導かれる改訂である。

## 0. 判定

```yaml
market_level_abstention: not_found
candidate_supply_is_scarce: not_supported
buyback_residue_inflates_ranking: not_supported
equity_anchor_cap_is_a_trap: not_supported
machine_top_outperforms_population: supported
published_thesis_read_path_is_broken: confirmed_and_fixed
```

## 1. 再現手順

```bash
uv run python -m tools.measure_signal_cohorts \
  --calibration-dir data/screening/calibration \
  --out .cache/833-signal-cohorts.yaml
```

観測日 2026-08-06。panel は `data/screening/calibration/panel-*.csv` の 80 か月（2019-11-29〜2026-06-30）で、全 80 本が `rules_hash: ad6f6b977fa75b0c`（現行 rules で再構築済み）。母集団は各 panel の流動性通過行（時価総額 ≥100 億円・平均売買代金 ≥1 億円・上場 182 日以上、`method/screening-rules/2026-07-06T000000+0900.yaml` の `selection.liquidity` と同値）で 107,059 行。forward は同 store の `forward-*.csv` の `status: resolved` 行のみ、price-only。

漏斗と保有・注文の事実は application DB（`shortlist` / `thesis` / `bargain_assessment` / `proposal` / `ledger_event`）と `data/screening/market.sqlite` から読んだ。

## 2. 漏斗の実測

| as-of | shortlist selected | research lane | assessment |
| --- | ---: | ---: | --- |
| 2026-07-17 | 8 | 0 | —（人間が primary-research set を選ばずに完了） |
| 2026-07-28 | 8 | 2 | no_actionable_bargain |
| 2026-07-29 | 8 | 3 | no_actionable_bargain |
| 2026-07-31 | 6 | 3 | no_actionable_bargain |
| 2026-08-03 | 3 | 3 | no_actionable_bargain |
| 2026-08-04 | 0 | 0 | —（shortlist 全 reject で完了） |

- research lane 11 本の 5y base CAGR は 2.43〜9.88%。要求 8.5% を超えたのは 1 本（6088、as-of 07-28 の 9.88%）だけで、as-of 08-03 の再研究で賞与引当戻入を正規化して 8.18% となり reject に反転した。
- lane のユニーク ticker は 6 で、11 lane 中 5 lane が直前 cycle の再研究である。
- `proposal` テーブルは 0 行。canonical ledger の最後の約定は 2026-07-15 の 3836。
- 2026-07-03 に人間が承認した 2 注文（2331 指値 1,050 / 8929 指値 1,190）は窓内で指値に一度も到達せず 07-31 に失効した。market store 実測で 2331 の期間最安値は 1,081（指値 +2.9%）・月末終値 1,173（+11.7%）、8929 は 1,247（+4.8%）・1,271（+6.8%）。
- ledger 実測の現金は 2,983,200 円（opening 4,871,100 − 取得 1,887,900）で、8/4 終値評価の保有 2,054,000 円に対し比率 59.2%。

## 3. 「あえての見送り」は記録上存在しない

macro context head（`macro-context-2026-07-31-yen-policy-floor-capex-proof`）は購入抑制を書いていない。`connection.bargain_topography` は割安が「AI 乱高下の陰の取り残し・円急伸の誤爆・決算直後の一時項目の誤読」に偏在すると名指しし、`sizing_cautions` は決算密集週と IV 97% 点を理由に「初回 lot は目安レンジの下限側」と言うだけである。

publish 済み assessment 4 本の reject / defer 理由は全件が銘柄別の算術（5y base CAGR と要求 8.5%、研究 FV と終値）で、market-level のリスクを主因にした lane は 0 だった。**見送りは市況判断の結果ではなく、銘柄別算術の積み上げの帰結である。**

## 4. 候補供給は歴史的に普通

| 指標 | 現在値 | 80 か月分布での位置 |
| --- | ---: | --- |
| selection 上位 5 の平均 E[r]（2026-06-30 panel） | 10.86% | 52nd percentile（中央値 10.82%） |
| 同（2026-08-04 run の longlist 上位 5、shortlist 焼き込み値） | 10.05% | 15th percentile |
| `pass_screen` かつ流動性通過かつ E[r] ≥ 8.5% の件数（2026-06-30 panel） | 13 | 76th percentile（中央値 7） |

供給が構造的に厚かったのは 2020-03〜2021-01（上位 5 平均 15.5〜21.0%、hurdle 超え 27〜91 件）だけで、2021-06 以降 61 か月の上位 5 平均は 9.2〜14.1% に収まる。現在はその中央帯にいる。`reports/2026-07-30-broad-decline-deployment-premise.md` の「下落局面でも候補は増えない（相関 +0.026）」とも整合する。

## 5. 機械上位は母集団に対して一貫して上回った

E[r] ≥ 8.5% の群と、同じ as-of の流動性通過母集団の実現年率中央値（price-only）。

| horizon | 高 E[r] 群 | 母集団 | 差 | cohort 一致 |
| --- | ---: | ---: | ---: | --- |
| 1y | +21.15%（n=1,407） | +5.39%（n=88,474） | +15.8pt | 38/46 |
| 3y | +16.88%（n=1,108） | +5.56%（n=53,861） | +11.3pt | **31/31** |
| 5y | +14.94%（n=828） | +5.65%（n=24,147） | +9.3pt | **18/18** |

cohort 差の中央値は 3y +11.71pt、5y +9.00pt。as-of 年別に見ても 2020〜2023 のすべてで正で、COVID 期（as-of 2019-11〜2021-06）を除いた 3y でも中央値 +17.7%（n=275）・75% 以上の名前が 8.5% を超えている。

**絶対水準は regime に依存する**（forward 窓はすべて 2023〜2026 の日本株上昇局面に入る）。regime を統制した量は「同じ as-of の母集団との差」であり、そこが 3y/5y で全 cohort 一致している点が本節の主張である。

## 6. 上流 E[r] の 2 つの水増し仮説は否定された

### 6.1 buyback carry の「残像」（#811 の前提）

carry の buyback 成分は `clip(-net_share_change_yoy, ±5%)` で、取得枠の現在状態を見ない。EDINET 自己株券買付状況報告書（doc_type 220、`data/screening/market.sqlite` に 2025-08-01 以降 6,319 件）で as-of 2026-08-04 の状態を突き合わせると、8/4 shortlist 20 銘柄のうち 16 銘柄が clip 5% に貼り付き、**12 銘柄は buyback carry > 0.5% でありながら直近 75 日に 220 提出が無い**（4887 は 12 か月窓で提出ゼロ）。流動性通過の全候補でも、buyback carry > 0.5% の 291 銘柄中 active は 145（50%）にとどまる。

ここまでは #811 の観察どおりである。**しかし forward return は残像を支持しない。**

| horizon | clip 到達群 | 中間群 | 非正群 | clip − 非正 | cohort 一致 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1y | +12.24%（n=2,952） | +13.14% | +4.33% | +7.9pt | 56/69 |
| 3y | +13.72%（n=1,324） | +10.86% | +4.91% | +8.8pt | **44/45** |
| 5y | +11.21%（n=421） | +10.02% | +5.20% | +6.0pt | **21/21** |

さらに、EDINET 提出の有無は 2025-08 以降しか観測できないため、代理変数として panel の `share_count_reduction_streak`（連続 FY 数）で「単発の株数減少（= 枠終了型に近い）」と「継続」を分けた。

| horizon | streak=1（単発） | streak≥2（継続） | 差 | cohort 一致 |
| --- | ---: | ---: | ---: | --- |
| 1y | +11.72%（n=3,816） | +15.67%（n=2,687） | +3.2pt | 45/69 |
| 3y | +11.87%（n=1,923） | +13.29%（n=1,069） | +2.6pt | 26/45 |
| 5y | +9.96%（n=628） | +13.26%（n=535） | +4.1pt | 17/21 |

継続にはわずかな上乗せがあるが、**単発群も母集団（+4.9〜5.7%）を大きく上回る**。したがって「取得枠が終了した銘柄の buyback carry を落とす」変更は、実在する予測情報を削る方向に働く。#811 の段階 2（carry の減衰）は採らない。

### 6.2 赤字予想での PBR anchor（#819 の前提）

PER が使えず PBR 単独 anchor になった行のうち、reversion が cap（upside 0.50）に貼り付いた群を分けた。

| horizon | PBR 単独 × cap | PBR 単独 × cap 未満 | PER 有 × cap | 母集団 |
| --- | ---: | ---: | ---: | ---: |
| 1y | +17.02%（n=600） | −3.28% | +22.70% | +5.39% |
| 3y | +14.72%（n=496） | −1.59% | +12.54% | +5.56% |
| 5y | +14.42%（n=392） | −0.45% | +12.70% | +5.65% |

cap に当たる PBR 単独 anchor は母集団を +9pt 前後上回る。**「帳簿は残っているが収益力が消えた」という筋は、この座標では罠として現れない。** #819 の案 2（reversion の減衰）も採らない。

いずれも annotation（事実の提示）としては有効である。研究側が carry を「forward の現金還元」と読むか「資本配分の質のマーカー」と読むかは、取得枠の現在状態を知らないと決められない。

### 6.3 成分分解

| horizon | reversion Q1 → Q5 | carry Q1 → Q5 |
| --- | --- | --- |
| 3y | −7.51% → +10.61% | −9.44% → +14.58% |
| 5y | −6.17% → +11.31% | −7.75% → +12.86% |

carry 上位 1/3 に固定して reversion の分位を見ると 3y は +10.93% → +14.66%（Q2 以降ほぼ平坦）で、carry が支配的である。

## 7. 詰まりの所在

機械層は 8.5% 以上と見た群を 3y/5y の全 cohort で母集団より上に置いた。同じ時期の research 層は、その群から選んだ 11 lane の 5y base CAGR を 2.43〜9.88% と算定し、要求 8.5% に対して実質全件を棄却した。

この差は 2 つの独立した保守化の積である。

1. **正規化**: 一過性利益を剥がした利益を起点にする。
2. **終端倍率**: 再評価（multiple の回復）を base に置かない。

どちらも個別には正当な規律で、AP 系の失敗を防いでいる。ただし **8.5% は「この二重に保守化した基準」に対する要求として運用されており、その基準が実現値とどう対応するかは一度も計測されていない**。`reports/2026-08-01-er-level-calibration.md` は機械 E[r] 自体が最上位 quintile で実現を 3y +11.99pt / 5y +10.94pt 下回る（= 予測が過小）ことを示しており、research 層はその機械 E[r] よりさらに保守側にある。

したがって本プログラムの改訂は次の 3 点に絞る。

1. **上流の E[r] は変えない。** 6.1 / 6.2 の計測が変更を支持しない。取得枠と赤字予想は annotation として提示し、research 側が carry の意味を判断できるようにする。
2. **機械層と research 層の乖離を判断面に出す。** 研究 5y base CAGR と、その銘柄が属する機械 E[r] 帯の歴史実現中央値を並べ、8.5% を素で当てる運用から、乖離を見たうえでの人間裁定に変える。
3. **境界帯に bounded な行動経路を開くかをオーナーが裁定する。** 5〜6 節の evidence は「機械上位を全件見送る」現状の期待値コストが小さくないことを示す。ただし縮小 lot・上限・撤退基準を伴わない緩和はしない。

## 8. 付随して確定した欠陥（修正済み）

`deep_discount_bps` は 2026-07-29 の指値ラダー退役（commit `1a681925`）で thesis schema から外れたが、退役前に published された 4 本（3836・4432・6345・6088）の payload には key が残っていた。published payload は `thesis_core_sha256` で review と assessment に束縛されるため書き換えられず、読み側が strict に拒否したことで次の経路が全て停止していた。

- `position holding-review-build`: 保有 10 件のうち 3836・4432 の 2 件が review 不能（`extra_forbidden` で終了）。
- `tools/research_price_watch`: 全体が起動不能（最初に当たった 4432 で終了）。
- `research assessment-scaffold` / `assessment-publish` / `proposal create`: 該当 thesis を参照する場合に同じ経路で停止。

退役 field を「null のみ受け、serialize せず、読み込んだ payload が key を持っていた場合だけ hash に書き戻す」形で復元し、両経路の復旧を実データで確認した。2121 の watch task が cycle 完了時に dropped されたのは、この tool が動かない状態で watch を人手の task で代替していたためである。

## 9. 誠実性の限定

- forward 窓は重なり独立でない。有意性・統計的優位・track record を主張しない。効果量と cohort 一致数で読む。
- price-only であり配当を含まない。総合リターンはこれより高い。
- 実現値は 2023〜2026 の日本株上昇局面を含む。regime 統制された量は同一 as-of の母集団との差だけである。
- 群の中央値は約 400〜88,000 行の記述統計であり、少数銘柄へ集中した portfolio の結果ではない。
- 廃止で系列が切れた銘柄は resolved に入らない。`delisting_exclusion` の向き安定判定は本 report の座標では行っていない。
- EDINET 220 の観測は 2025-08-01 以降のみ。それ以前の「提出なし」は不在ではなく未観測である。
- 6.1 の streak 代理変数は取得枠の状態そのものではない。

## 10. 監視事項

- 2026-10-17 に shortlist 判断 cohort の初回 3m 採点が可能になる。selected / rejected / machine の中央超過を記録する。
- panel が 1 か月増えるたびに 5 節と 6 節を再計算する。特に 5y の cohort 数（現在 18〜21）は結論の頑健性に直結する。
- EDINET 220 の観測窓が 2 年に伸びたら、6.1 の streak 代理変数を実際の取得枠状態へ置き換えて再検証する。
