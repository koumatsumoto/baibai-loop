---
title: "株主還元の変化軸の事前登録"
summary: "低 PER 帯で増配・還元開始・株数減少の変化が 1y/3y excess と trap を改善するかを測る事前登録。"
doc_type: report
status: active
date: 2026-08-02
---

# 株主還元の変化軸の事前登録

価値tier: T1 — 低 valuation 候補のうち還元が改善している銘柄を識別し、バリュートラップ率を下げる。

本書は #718 の forward return を計測する前に、入力境界、列、方向、時間分割、交絡確認、採否条件を固定する。実装後の数値を見て定義、閾値、符号、窓、統計量を変更しない。逆方向または基準未達は `negative`、標本不足は `insufficient` とする。

## 1. 入力境界と正規化

既存の `FIN_INPUT_WINDOW_DAYS=730` は TTM・valuation・E[r] の production/replay 同一性を守るため変更しない。還元変化列だけ、calibration panel が as-of 以前1200暦日の `jquants_fin_summaries` を補助入力として読む。補助入力は `FinancialSnapshot`、candidate metrics、gate、ranking、E[r] へ渡さない。

実績系列は `fiscal_period == "FY"` かつ `fiscal_year_end` がある行を対象とし、同じ fiscal year end の revision は as-of 以前で最も遅い disclosure だけを使う。選んだ revision の対象値が null の場合、古い revision の非 null 値へ戻らない。3 FY は fiscal year end が同月同日の1年刻みで連続することを要求し、決算期変更を跨ぐ系列は欠損とする。

DPS は既存 `_normalize_summaries_to_asof_basis` で開示後から as-of までの分割・併合を現在株式基準へ揃える。各 FY 実績 DPS はさらに、前 FY 実績の disclosure から当 FY disclosure までの adjustment factor を掛ける。前 FY 行が補助窓に無い最古行だけ、既存 carry と同じ400日 fallback を accrual 開始に使う。負値、非有限値、基準を判定できない予想 DPS は欠損とし、0は有効値とする。

`shares_outstanding` は既存 normalization 後の値を使う。この field は自己株式込みのグロス発行済株式数であり、減少は自己株取得そのものを示さない。したがって列名と解釈は `buyback_streak` ではなく `share_count_reduction_streak` とし、分割・併合を除いた消却・発行等の純変化 proxy に限定する。

## 2. 固定する panel 列

| PanelRow field | 型・定義 | 仮説方向 |
| --- | --- | --- |
| `dps_streak_up` | `bool | None`。最新3 FYの実績DPSが古い順に単調非減少なら true。3 FYを完全に観測できなければ null | true が良い |
| `dps_yoy_latest` | `float | None`。`latest / prior - 1`。prior > 0を要求する。両期0は0、0→正は initiation で表し本列は null | 高いほど良い |
| `dps_guidance_up` | `bool | None`。最新実績 disclosure 以後の最新 split-safe 予想DPSが最新実績DPSを厳密に上回る | true が良い |
| `dividend_initiation` | `bool | None`。直前実績0→最新実績正、または最新実績0→予想正。必要な比較値が無ければ null | true が良い |
| `share_count_reduction_streak` | `int | None`。最新3 FYのグロス株数について、新しい側から連続する前年比減少回数。0〜2。3 FYを完全に観測できなければ null | 高いほど良い |
| `shareholder_return_change` | `bool | None`。最新実績DPS増、`dps_guidance_up`、`dividend_initiation`、直近株数減少のいずれかが true なら true。4条件をすべて判定でき全て false のときだけ false。それ以外は null | true が良い |

実績DPS増は最新・直前実績が非負で `latest > prior`、直近株数減少は `share_count_reduction_streak >= 1` とする。`dps_streak_up` は安定還元の副軸であり、横ばい3期だけでは変化 composite を true にしない。

generic axis evaluator には、連続量/順序量である `dps_yoy_latest` と `share_count_reduction_streak` をそれぞれ `direction=1` で追加する。bool 4列は tie の多い decile へ押し込まず、yes/no の群比較で読む。

## 3. 主検定

forward return は既存 `price_return_only`、excess は同 cohort の resolved 流動性母集団中央値との差、trap は `excess < -0.20` を使う。

各 cohort の `in_population` かつ resolved forward return かつ正の `per_trailing` がある行を PER 昇順に並べ、既存 decile と同じ安定した境界で最良2 decile（低 PER 20%）を `low_valuation_band` とする。その中の `shareholder_return_change == true` を change、false を no-change とし、null は除く。

固定列は次のとおり。

- `shareholder_return_change.low_valuation_n`
- `shareholder_return_change.eligible_n`
- `shareholder_return_change.change.{n,median_excess,mean_excess,trap_rate}`
- `shareholder_return_change.no_change.{n,median_excess,mean_excess,trap_rate}`
- `shareholder_return_change.median_excess_delta` = change − no-change
- `shareholder_return_change.mean_excess_delta` = change − no-change
- `shareholder_return_change.trap_rate_delta` = change − no-change

cohort 横断は、delta を算出できる cohort の単純平均、`median_excess_delta > 0` の cohort share、両群の合計 n を使う。有意性検定、重複する monthly cohort を独立標本とみなす推測統計、閾値探索は行わない。

副検定として、`dps_yoy_latest` と `share_count_reduction_streak` の既存 axis 統計、および low valuation band 内で `dps_streak_up` / `dps_guidance_up` / `dividend_initiation` の各 true−false 群差を同じ統計量で報告する。副検定の一部だけ良い場合も composite の不通過を上書きしない。

## 4. 非重複の時間分割

| horizon | design cohort as-of | confirm cohort as-of | 標本条件 |
| --- | --- | --- | --- |
| 1y | 2019-11-29〜2022-12-30 | 2024-01-31〜2025-06-30 | 各窓で両群合計 n が各100以上、delta 算出可能 cohort が6以上 |
| 3y | 2019-11-29〜2020-05-29 | — | 両群合計 n が各15以上、delta 算出可能 cohort が3以上 |

1y design の exit は2023年末まで、confirm entry は2024年以降で重ならない。3y は現時点で独立 confirm に使える十分な満期・authority cohort が無いため design の長期方向確認だけを事前登録する。3y design を confirm と呼び替えず、production 参入には別 issue で独立3y confirmと5yを要求する。

## 5. 交絡確認

主検定と同じ low valuation band・change/no-change を、次の順で**すべて**独立に二分位層別する。各 control の非欠損行を cohort 内中央値で `<= median` / `> median` に分け、両群が各5行以上ある strata だけを使う。stratum の重みは `min(change_n, no_change_n)` とし、change−no-change delta を重み付き平均する。交差 strata は作らない。

| order | control field | 想定する交絡 |
| ---: | --- | --- |
| 1 | `dividend_yield` | carry 水準 |
| 2 | `per_trailing` | low valuation band 内の割安度 |
| 3 | `pbr` | 資産価値ベースの割安度 |
| 4 | `market_cap_oku` | 規模 |
| 5 | `avg_turnover_oku` | 流動性 |
| 6 | `price_change_60d` | momentum / reversal |

固定列は `shareholder_return_change.controls.<field>.{strata_used,matched_weight,stratified_median_excess_delta,stratified_trap_rate_delta}` とする。1y design / confirm の双方で全6 control を最後まで計算する。

## 6. 採否基準

次をすべて満たす場合だけ `adoption_candidate` とする。

1. 1y design / confirm の双方で主検定の `median_excess_delta >= 0.03`、`trap_rate_delta < 0`、positive cohort share > 0.5。
2. 3y design で主検定の `median_excess_delta > 0`、`trap_rate_delta < 0`、positive cohort share > 0.5。
3. 1y design / confirm の双方で6 controlすべての `stratified_median_excess_delta > 0` かつ `stratified_trap_rate_delta <= 0`。
4. `dps_yoy_latest` と `share_count_reduction_streak` のうち少なくとも一方が、1y design / confirm と3y designの eligible aggregateすべてで axis decile spread 正。
5. §4 の標本条件と現行 calibration authority/coverage 判定を満たす。blocker cohort は除外数と理由を報告する。

eligible な窓で方向または効果量を外せば `negative`、必要窓が標本・authority 未達なら `insufficient` とする。`adoption_candidate` でも本 issue では panel 証跡だけを残し、candidate metrics、research annotation、gate、ranking、E[r] を変更しない。production 採用は独立3y confirmと5yを固定する別 issue に分離する。negative の場合も panel 列は再現可能な計測証跡として残すが、判断面へ昇格させない。

## 7. 検算と限定

- revision は最新行を優先し、最新 null から古い非 null へ戻らない fixture を置く。
- 1:2分割を実績 FY 間・最新開示後に置き、3 FY DPS と株数が同じ as-of basis になることを検算する。
- DPS の増加、横ばい、減配、0→正、予想増配、予想欠損と、株数 streak 0/1/2、3 FY欠損を検算する。
- panel CSV round-trip、optional bool の不正 token、streak 範囲外、composite と成分の矛盾を fail-closed にする。cache schema を更新し、`docs/anti-patterns.md` AP-08 に cross-field validation を反映する。
- 代表 cohort の low valuation band 境界、両群 n、median、trap 件数を独立計算する。
- `price_return_only` は実現配当を含まない。DPS変化は価格 excess との関連を測るもので、total shareholder return の直接測定ではない。
- グロス株数減少は自己株取得の一次事実ではない。採用候補になっても、実際の自己株取得・消却は一次IRで確認する。
