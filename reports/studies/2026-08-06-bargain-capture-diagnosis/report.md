---
title: "お買い得捕捉経路の診断 — ゼロ購入 6 cycle の所在"
summary: "6 cycle 連続ゼロ購入の記録を漏斗別に点検した。機械評価と研究評価の乖離を観測したが、原因の分離や取得枠終了の効果は未検証。PBR anchor の結果は標本窓で反転する。"
doc_type: measurement-record
status: historical
---

# お買い得捕捉経路の診断

2026-08-06 時点の旧 screening・Thesis・starter band に関する履歴。以下の現行 rules、修正済み経路、監視事項は当時のもので、現行の Stock Assessment / Investment Proposal の手順や未完了作業を定義しない。

価値tier: T1 — 「割安を拾えていない」の原因を漏斗の段ごとに特定し、どの層を直せば拾える確率が上がるかを実データで決める。

対象 issue: [#833](https://github.com/koumatsumoto/baibai-loop/issues/833)。#833 は診断仮説として「上流 E[r] の残像汚染」を第 1 要因に置いていたが、**本計測ではその仮説の採否を確定できない**。以下は計測と、そこから導かれる改訂である。

## 0. 判定

```yaml
market_level_abstention: not_found
monthly_panel_hurdle_clearing_count: 76th_percentile
daily_run_top5_expected_return: 15th_percentile
buyback_residue_inflates_ranking: not_supported
equity_anchor_cap_is_a_trap: inconclusive
machine_top_outperforms_population: supported_with_regime_caveats
published_thesis_read_path_is_broken: confirmed_and_fixed
```

供給は 2 座標に割れており、1 語へ畳まない（§4）。件数は最新月末 panel 基準の遅行座標、top-5 は当日 run と同じ基準の座標である。`equity_anchor_cap_is_a_trap` は当初 `not_supported` としたが、COVID entry を外すと符号が反転するため判定不能へ戻した（§6.2）。`machine_top_outperforms_population` の pooled 差は非 COVID 窓でも残るが、cohort 一致数は閾値と標本窓に依存する（§5）。

## 1. 再現手順

```bash
uv run python -m tools.experiments.measure_signal_cohorts \
  --calibration-dir stores/screening/calibration \
  --out .cache/833-signal-cohorts.yaml
# COVID entry を外した窓（§5・§6.1・§6.2 の反転確認に使う）
uv run python -m tools.experiments.measure_signal_cohorts \
  --calibration-dir stores/screening/calibration \
  --asof-from 2021-07-01 --out .cache/833-signal-cohorts-post-covid.yaml
```

観測日 2026-08-06。panel は `stores/screening/calibration/panel-*.csv` の 80 か月（2019-11-29〜2026-06-30）で、全 80 本が `rules_hash: ad6f6b977fa75b0c`（現行 rules で再構築済み）。母集団は各 panel の流動性通過行（時価総額 ≥100 億円・平均売買代金 ≥1 億円・上場 182 日以上、`method/screening/rules/2026-07-06T000000+0900.yaml` の `selection.liquidity` と同値）で 107,059 行。forward は同 store の `forward-*.csv` の `status: resolved` 行のみ、price-only。

漏斗と保有・注文の事実は application DB（`shortlist` / `thesis` / `bargain_assessment` / `proposal` / `ledger_event`）と `stores/market/market.sqlite` から読んだ。

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

publish 済み assessment 4 本の reject / defer 理由は全件が銘柄別の算術（5y base CAGR と要求 8.5%、研究 FV と終値）で、market-level のリスクを主因にした lane は 0 だった。**記録された直接の見送り理由は銘柄別評価だった。** 市況が評価入力や選定に与えた間接的影響まで除外した結果ではない。

## 4. 候補供給は歴史的に普通

| 指標 | 現在値 | 80 か月分布での位置 |
| --- | ---: | --- |
| selection 上位 5 の平均 E[r]（2026-06-30 panel） | 10.86% | 52nd percentile（中央値 10.82%） |
| 同（2026-08-04 run の longlist 上位 5、shortlist 焼き込み値） | 10.05% | 15th percentile |
| `pass_screen` かつ流動性通過かつ E[r] ≥ 8.5% の件数（2026-06-30 panel） | 13 | 76th percentile（中央値 7） |

供給が構造的に厚かったのは 2020-03〜2021-01（上位 5 平均 15.5〜21.0%、hurdle 超え 27〜91 件）だけで、2021-06 以降 61 か月の上位 5 平均は 9.2〜14.1% に収まる。月末 panel の上位5平均はその範囲内にあるが、当日 run の比較順位は15th percentileであり、両座標を一括して「中央帯」としない。[7月30日の調査](../2026-07-30-broad-decline-deployment-premise/report.md) で報告した相関 +0.026 は、その標本内で線形関連が弱いという記述に限り、下落局面の候補増加が無いことを証明しない。

## 5. 高 E[r] 群と母集団の比較

E[r] ≥ 8.5% の群と、同じ as-of の流動性通過母集団の実現年率中央値（price-only）。

| horizon | 高 E[r] 群 | 母集団 | 差 | cohort 一致 |
| --- | ---: | ---: | ---: | --- |
| 1y | +21.15%（n=1,407） | +5.39%（n=88,474） | +15.8pt | 38/46 |
| 3y | +16.88%（n=1,108） | +5.56%（n=53,861） | +11.3pt | **31/31** |
| 5y | +14.94%（n=828） | +5.65%（n=24,147） | +9.3pt | **18/18** |

cohort 差の中央値は 3y +11.71pt、5y +9.00pt。COVID entry（as-of 2019-11〜2021-06）を除いても pooled の差は残る: 1y +19.84%（n=560）対 +6.58%、3y +17.65%（n=275）対 +7.33%。

**cohort 一致数は閾値と標本窓に依存する。** 群比較は treatment と母集団の両方が最小件数を満たす as-of だけを数えるので、候補が薄い月ほど落ちる。

| horizon | min 3 | min 5 | **min 10（上表）** | min 20 |
| --- | --- | --- | --- | --- |
| 1y | 55/69 | 54/68 | **38/46** | 15/17 |
| 3y | 43/45 | 43/45 | **31/31** | 14/14 |
| 5y | 21/21 | 21/21 | **18/18** | 14/14 |

3y で閾値 5 のときに負ける 2 cohort は 2023-04-28（−7.30pt）と 2023-07-31（−4.05pt）で、**3y が解決している 45 as-of のうち最も新しい 2 つ**である。1y は as-of 年別に見ると 2022 が +19pt 台（12/12）に対し 2024 は +1.25pt（7/12）で、2024-08〜10 と 2025-02〜03 は負である。**「全 cohort 一致」を単独の見出し数値として使わない。**

**5y 列は独立確認ではない。** 5y が解決する as-of は 2019-11-29〜2021-07-30 の 21 個しかなく、treatment 828 行のうち 671 行（81%）が 2020-02〜2021-01 entry である。3y の 45 as-of とも入れ子で、同じ entry を 2 つの horizon で測っている。「2020 年の暴落で割安株を買って 2025-26 まで持った」という 1 つの事実として読む。

**絶対水準は regime に依存する**（forward 窓はすべて 2023〜2026 の日本株上昇局面に入る）。同じ as-of の比較は共通の観測時期を揃えるが、業種・規模・銘柄構成や相場感応度の差は除いていない。pooled では群ごとの as-of 構成比も異なり得る。非 COVID 窓の正の差は記述的な群差であって、regime を除去した効果ではない。

## 6. 上流 E[r] の水増し仮説を判定する際の限界

### 6.1 buyback carry の「残像」（#811 の前提）

carry の buyback 成分は `clip(-net_share_change_yoy, ±5%)` で、取得枠がいつまで在ったかを見ない。EDINET 自己株券買付状況報告書（doc_type 220、`stores/market/market.sqlite` に 2025-08-01 以降 6,319 件）で as-of 2026-08-04 の提出を突き合わせると、8/4 shortlist 20 銘柄のうち 16 銘柄が clip 5% に貼り付き、**12 銘柄は buyback carry > 0.5% でありながら直近 75 日に 220 提出が無い**（4887 は 12 か月窓で提出ゼロ）。流動性通過の全候補でも、buyback carry > 0.5% の 291 銘柄中で直近 75 日に提出があるのは 145（50%）にとどまる。

ここまでは #811 の観察どおりである。**以下の forward return は株数減少の代理群の比較であり、取得枠終了による E[r] の過大評価を直接検証するものではない。**

| horizon | clip 到達群 | 中間群 | 非正群 | clip − 非正 | cohort 一致 | 株数変化 未観測 |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| 1y | +12.24%（n=2,952） | +13.14% | +4.48% | +7.8pt | 55/69 | +1.78%（n=4,834） |
| 3y | +13.72%（n=1,324） | +10.86% | +5.09% | +8.6pt | **44/45** | +1.67%（n=3,180） |
| 5y | +11.21%（n=421） | +10.02% | +5.39% | +5.8pt | **21/21** | +1.70%（n=1,487） |

`net_share_change_yoy` の欠測行は control から外している。0 と読むと「株数が動かなかった」と「株数変化が分からない」が同じ群に入るが、未観測群の実現は +1.7% 前後で両者とは別物である。

さらに、EDINET 提出の有無は 2025-08 以降しか観測できないため、代理変数として panel の `share_count_reduction_streak`（連続 FY 数）で「単発の株数減少（= 枠終了型に近い）」と「継続」を分けた。

| horizon | streak=1（単発） | streak≥2（継続） | 表示中央値の差（継続−単発） | cohort 一致 |
| --- | ---: | ---: | ---: | --- |
| 1y | +11.72%（n=3,816） | +15.67%（n=2,687） | +3.95pt | 45/69 |
| 3y | +11.87%（n=1,923） | +13.29%（n=1,069） | +1.42pt | 26/45 |
| 5y | +9.96%（n=628） | +13.26%（n=535） | +3.30pt | 17/21 |

継続にはわずかな上乗せがあるが、**単発群も母集団（+4.9〜5.7%）を大きく上回る**。COVID entry を外しても clip 群は母集団を上回る（1y +13.19% 対 +5.37%、3y +14.73%（n=909）対 +6.46%）。確認した二つの窓では差の符号が同じだったが、取得枠終了群を識別した結果ではなく、carry 減衰による順位・成績の変化も測っていない。#811 の段階 2 を当時採らなかった判断と、減衰が予測情報を失わせるとの因果主張は区別する。

### 6.2 赤字予想での PBR anchor（#819 の前提）— 判定不能

PER が使えず PBR 単独 anchor になった行のうち、reversion が cap（upside 0.50）に貼り付いた群を分けた。全期間を pooled すると母集団を大きく上回るが、**COVID entry を外すと符号が反転する**。

| horizon | 全 as-of の treatment | as-of > 2021-06-30 の treatment | 同 as-of の母集団 |
| --- | ---: | ---: | ---: |
| 1y | +17.02%（n=600） | **−11.87%（n=201）** | +6.58% |
| 3y | +14.72%（n=496） | **−8.37%（n=101）** | +7.33% |
| 5y | +14.42%（n=392） | +14.30%（n=3、判定不能） | +6.85% |

pooled の 3y treatment 496 行のうち 2020-02〜2021-01 entry が大半を占める。cohort 比較が成立する as-of も暴落月に偏る（この群が 10 名以上いる月がそこに集中するため）。同じ非 COVID の as-of 範囲で比較すると、**非 COVID 窓ではこの群は母集団を下回る**。

したがって「帳簿は残っているが収益力が消えた」という筋がこの座標で罠にならない、とは言えない。**#819 の案 2（reversion の減衰）は不採用ではなく保留とする。** 判定には非 COVID の 3y/5y cohort が要る。

群の定義にも限界がある。`per_forward` / `per_trailing` は panel に負値が 1 件も保存されておらず、「赤字」と「欠測」が同じ扱いに畳まれている。cap 群には 3 期正規化 PER が正（直近だけ赤字）の行も配当を出している行も含まれ、#819 が想定した母集団と一致しない。

### 6.3 annotation としての価値

6.1 / 6.2 のどちらも、annotation（事実の提示）としては有効である。研究側が carry を「forward の現金還元」と読むか「資本配分の質のマーカー」と読むかは、取得枠の現在状態を知らないと決められない。

### 6.4 成分分解

| horizon | reversion Q1 → Q5 | carry Q1 → Q5 |
| --- | --- | --- |
| 3y | −7.51% → +10.61% | −9.44% → +14.58% |
| 5y | −6.17% → +11.31% | −7.75% → +12.86% |

carry 上位 1/3 に固定して reversion の分位を見ると 3y は +10.93% → +14.66%（Q2 以降ほぼ平坦）だった。この条件付き分位比較だけでは、carry と reversion の独立寄与や支配関係までは分離できない。

## 7. 詰まりの所在

機械層の E[r] 8.5%以上群は、§5 の最小群サイズ10の条件で残った3y/5y cohortでは母集団を上回った。閾値を変えると3yには負の差もある。一方、今回の research 層は、当時の候補から選んだ 11 lane の 5y base CAGR を 2.43〜9.88% と算定し、要求 8.5% に対して実質全件を棄却した。

この差に関係し得る研究上の仮定は次の二つである。独立性やそれぞれの寄与を実験で分離したわけではない。

1. **正規化**: 一過性利益を剥がした利益を起点にする。
2. **終端倍率**: 再評価（multiple の回復）を base に置かない。

どちらも当時の評価規律だが、失敗を防いだ効果を本計測は検証していない。ただし **8.5% は「この二重に保守化した基準」に対する要求として運用されており、その基準が実現値とどう対応するかは一度も計測されていない**。[8月1日の E[r] 較正](../2026-08-01-er-level-calibration/report.md) は機械 E[r] 自体が最上位 quintile で実現を 3y +11.99pt / 5y +10.94pt 下回る（= 予測が過小）と報告している。ただし、歴史的 quintile と今回の研究対象は異なる標本・評価条件であり、この差を研究評価の上方補正値には使えない。

以下は当時の改訂案であり、上の異なる標本の比較だけで緩和の有効性が確認されたものではない。

1. **上流の E[r] は変えない。** 6.1 は窓を変えても carry 減衰を支持せず、6.2 は判定不能である（不採用の根拠が無い代わりに、採用の根拠も無い）。取得枠と赤字予想は annotation として提示し、research 側が carry の意味を判断できるようにする。#819 の案 2 は非 COVID cohort が揃うまで保留する。
2. **機械層と research 層の乖離を判断面に出す。** 研究 5y base CAGR と、その銘柄が属する機械 E[r] 帯の歴史実現中央値を並べ、8.5% を素で当てる運用から、乖離を見たうえでの人間裁定に変える。
3. **境界帯に bounded な行動経路を開くかをオーナーが裁定する。** 5〜6 節は見送り対象を追加検証する動機にはなるが、実際の選定・購入価格・資本制約を反映した反実仮想 portfolio はなく、見送りの期待値コストは推定していない。ただし縮小 lot・上限・撤退基準を伴わない緩和はしない。

## 8. 付随して確定した欠陥（修正済み）

`deep_discount_bps` は 2026-07-29 の指値ラダー退役（commit `1a681925`）で thesis schema から外れたが、退役前に published された 4 本（3836・4432・6345・6088）の payload には key が残っていた。published payload は `thesis_core_sha256` で review と assessment に束縛されるため書き換えられず、読み側が strict に拒否したことで次の経路が全て停止していた。

- `position holding-review-build`: 保有 10 件のうち 3836・4432 の 2 件が review 不能（`extra_forbidden` で終了）。
- `baibai_engine.research_watch`: 全体が起動不能（最初に当たった 4432 で終了）。
- `research assessment-scaffold` / `assessment-publish` / `proposal create`: 該当 thesis を参照する場合に同じ経路で停止。

退役 field を「null のみ受け、serialize せず、読み込んだ payload が key を持っていた場合だけ hash に書き戻す」形で復元し、両経路の復旧を実データで確認した。2121 の watch task が cycle 完了時に dropped されたのは、この tool が動かない状態で watch を人手の task で代替していたためである。

## 9. 誠実性の限定

- forward 窓は重なり独立でない。有意性・統計的優位・track record を主張しない。効果量と cohort 一致数で読む。
- **cohort 一致数は最小群サイズの閾値が選ぶ標本に依存する。** 閾値は treatment が痩せる月＝候補が薄い月を落とすので、単独の見出し数値として使わない（§5 に感度表）。
- **entry 側が偏っている。** 3y の treatment は 62%、5y は 81% が 2020-02〜2021-01 entry である。exit 側が上昇局面であることだけを断ると、読者は 5y 列を「長期でも独立に成立」と読む。
- **price-only であり配当を含まない。** store は FY 実績配当を加えた total return を horizon により 88〜93% の被覆で持つが、被覆が horizon で変わるため本 report は price-only を主 basis にした。FY 実績配当の加算は実際の保有期間の受取額とは限らず、欠測による群構成差もある。配当を加えた群中央値の差や結論の符号が必ず維持されるとは言えない。
- 2 座標を並べる場所（§4）では、両者の percentile が同じ ranking 軸に載っていない。件数は月末 panel の集合、top-5 は selection rank 上位の平均で、母数の作り方が違う。
- 実現値は 2023〜2026 の日本株上昇局面を含む。同一 as-of の母集団との差でも業種・規模・相場感応度の差は残る。この読み方を当てると §6.2 は反転する。
- 群の中央値は約 400〜88,000 行の記述統計であり、少数銘柄へ集中した portfolio の結果ではない。
- 廃止で系列が切れた銘柄は resolved に入らない。`delisting_exclusion` の向き安定判定は本 report の座標では行っていない。
- EDINET 220 の観測は 2025-08-01 以降のみ。それ以前の「提出なし」は不在ではなく未観測である。
- 6.1 の streak 代理変数は取得枠の状態そのものではない。

## 10. 監視事項

- 2026-10-17 に shortlist 判断 cohort の初回 3m 採点が可能になる。selected / rejected / machine の中央超過を記録する。
- panel が 1 か月増えるたびに 5 節と 6 節を **全期間と `--asof-from 2021-07-01` の両方で**再計算する。特に §6.2 は非 COVID の 3y cohort が 10 を超えたら再判定する（現在 n=101 行・cohort 数不足）。
- §5 の cohort 一致は最小群サイズの感度とセットで更新する。3y で負けた 2 cohort（2023-04-28 / 2023-07-31）が最新側であることは、今の regime に近い月ほど差が縮む可能性を示す。
- EDINET 220 の観測窓が 2 年に伸びたら、6.1 の streak 代理変数を実際の取得枠状態へ置き換えて再検証する。
- **starter band の撤退基準 (b) の初回判定は、最初の starter 約定から 1 年後**である。`proposal.payload` の `position_intent` / `starter_catalyst_date` と `ledger_event.proposal_id` から cohort を組み、full cohort と並べる。件数が 1 桁のうちは中央超過を算出せず、並べるだけにする。約定が 0 件のままなら「帯を開いても通らなかった」という結論であり、その場合は band の条件側を見直す。
