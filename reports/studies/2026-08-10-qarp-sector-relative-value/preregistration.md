---
title: "QARP sector相対割安残差レーンの事前登録"
summary: "quality gate後の全流動性母集団をsector fixed effectとsizeで調整し、core外候補の増分をmatched boundaryと比較する条件を固定する。"
doc_type: report
status: active
date: 2026-08-10
---

# QARP sector相対割安残差レーンの事前登録

価値tier: T1 — core E[r] と異なる選択幾何が、core top-20 外から trap 非劣後かつ正の増分価値を持つ候補を安定供給できるかを、production 配線前に判定する。

本書は QARP の forward outcome を読む前に、入力列、quality gate、sector・size 統制、順位、比較対象、時間窓、重み、sufficiency、effect、verdict、cleanup を固定する。結果後に閾値、窓、重み、fallback、variant を変更しない。

## 1. 事前 coverage

`jquants_fin_summaries` と既存 panel だけを read-only で結合し、既存 `TTMRules` と as-of 以前 730 暦日の入力窓で level 列の可用率を測った。forward CSV と realized return は読んでいない。

| 対象 | panel 数 | min | median | latest (2026-06-30) |
| --- | ---: | ---: | ---: | ---: |
| `operating_profit_to_assets` | 78 | 67.8% | 80.2% | 88.4% |
| `operating_margin` | 78 | 67.8% | 80.0% | 88.2% |
| `asset_turnover` | 78 | 72.9% | 84.8% | 94.0% |
| 3 列同時 | 78 | 67.8% | 80.0% | 88.2% |

`in_population >= 100` の 2020-01〜2026-06 を対象にする。2019-11 / 2019-12 は population が 1 / 21 のため study 母集団へ入れない。coverage 不足を quality gate や採否閾値の緩和で救済しない。

## 2. PIT profitability level

各列は cohort as-of 以前 730 暦日の `jquants_fin_summaries` だけから作る。flow は既存 TTM 合成 `latest cumulative + prior FY - prior same period` を使い、通期行はそのまま使う。`operating_profit` が欠ける場合に `ordinary_profit` / `profit` へ fallback しない。`total_assets` は同じ PIT 窓の直近 non-null 開示を carry-forward する。

| panel field | 定義 | 欠損 |
| --- | --- | --- |
| `operating_profit_to_assets` | `operating_profit_ttm / total_assets` | operating profit TTM または正の assets が無い |
| `operating_margin` | `operating_profit_ttm / sales_ttm` | operating profit TTM または正の sales TTM が無い |
| `asset_turnover` | `sales_ttm / total_assets` | sales TTM または正の assets が無い |

将来開示、非有限値、ゼロ以下の分母は値へ補完しない。3 列が揃う行は `operating_profit_to_assets = operating_margin × asset_turnover` を丸め前で満たす。

## 3. 固定 QARP membership

### 3.1 母集団と quality gate

母集団は `in_population: true`、`population_coverage_status: evaluated` の行である。次をすべて満たす行だけを quality-passed とする。

- `accruals_to_assets < 0`
- `equity_ratio >= 0.40`
- `ocf_yield > 0`
- `operating_profit_yoy > -0.05`
- `operating_profit_to_assets > 0`
- `operating_margin > 0`
- `asset_turnover > 0`

`None` は不通過であり、0 や中央値へ補完しない。profitability 3 条件は同じ level fact の正値と導出被覆を固定するもので、加点スコアにはしない。`fcf_yield` は historical panel の full-population coverage を持たないため使わない。金融業等の sector を事後除外しない。

### 3.2 sector fixed effect + size residual

`market_cap_oku`、`pbr`、PER は正かつ有限を要求する。PER は正の `per_forward` を優先し、無ければ正の `per_trailing` を使う。どちらも無ければ QARP rank は付けない。

PBR と PER を別々に、quality-passed 集合で次の固定変換へ通す。

1. `x = ln(market_cap_oku)`、`y = ln(multiple)` とする。
2. 各 `sector_33` の `x` / `y` 平均を引く。
3. 全 sector を束ねた within-sector 共通 size slope `beta = Σ(x-x_sector_mean)(y-y_sector_mean) / Σ(x-x_sector_mean)^2` を求める。分母 0 はその metric を cohort 全体で unmeasured とする。
4. `residual = (y-y_sector_mean) - beta × (x-x_sector_mean)` とする。
5. residual を `(residual asc, ticker asc)` で ordinal percentile `[0, 1]` にし、PBR percentile と PER percentile の算術平均を `qarp_value_score` とする。

各 metric で sector 内 5 行未満の sector は residual 母集団と QARP membership から除く。2 metric の residual が揃う行だけを `(qarp_value_score asc, ticker asc)` で並べ、先頭 20 件を QARP top-20 とする。20 件未満ならその cohort は `candidate_unavailable`。core top-20 は同じ panel の `selection_rank <= 20` とし、QARP top-20 から core top-20 を除いた先頭 5 件を `qarp_outside_top5` とする。5 件未満なら `candidate_unavailable`。同値処理、PER fallback、sector floor の variant は試さない。

### 3.3 matched core boundary

各 `qarp_outside_top5` に対し、`selection_rank` 11〜30、`population_coverage_status: evaluated`、正の market cap を持つ未使用 ticker を pool とする。QARP ticker と同じ `sector_33` の中から `abs(ln(market_cap_oku_q) - ln(market_cap_oku_core))` が最小の行を選び、同値は `(selection_rank asc, ticker asc)` で解く。QARP score 順に greedy without replacement で 1 対 1 match する。

同 sector の相手が無い QARP 行は unmatched とし、別 sector や population median で代用しない。cohort は 5 QARP 行のうち 4 行以上が match した場合だけ pair effect を持つ。core top-20 全体の結果も比較表へ出すが、採否の matched pair を置き換えない。

## 4. 固定時間窓

entry は各 panel as-of、outcome は既存 forward store の `1y` / `3y` とする。

| window | horizon | cohort as-of | 位置づけ |
| --- | --- | --- | --- |
| `1y_design` | 1y | 2020-01-31〜2023-07-31 | retrospective design |
| `1y_time_holdout` | 1y | 2023-08-31〜2025-06-30 | 時系列後半 |
| `3y_all` | 3y | 2020-01-31〜2023-06-30 | 長期 aggregate |
| `3y_postcovid` | 3y | 2021-07-30〜2023-06-30 | COVID 初期 entry を除く感度 |

`3y_postcovid` は `3y_all` の部分集合で独立 confirm ではない。1y の forward 窓も calendar 上で重なるため、月次 cohort を独立標本とは呼ばない。窓境界は結果後に満期済みの都合のよい月へ動かさない。

## 5. return、excess、trap、欠損

- 主 basis は既存 split-adjusted `price_return_only`。total return は採否に使わず、price-only が配当を含まない限界を report に明記する。
- effect は各 row の累積 price return を `annualized = (1 + return)^(1 / horizon_years) - 1` へ変換する。`return == -1` は `annualized = -1`。`return < -1` または非有限値は integrity failure。
- `annualized_excess` は row の annualized return から同じ cohort の resolved `in_population` の annualized return 中央値を引く。
- pair delta は `QARP annualized_excess - matched core annualized_excess`。同じ cohort median を引くため return 差と同じだが、両側の excess を明示する。
- trap は既存研究と同じ累積 price excess `< -0.20`。QARP、matched core、core top-20 の rate を別々に出す。
- reported case は両 pair の price status が `resolved` の行だけを値へ入れる。membership と unmatched / unresolved status は残す。
- `unresolved_missing_exit` / `unresolved_stale_exit` は sensitivity で全損 `-1.0` と中立（同 cohort の resolved population median）を入れる。`entry_price_gap`、未来 horizon、未知 status、population median 不成立は代入せず integrity failure。

## 6. 主読みと集中

主読みは **QARP ticker 等重み**である。

1. 各 QARP ticker について window 内の pair delta、QARP excess、trap の cohort 中央値または率を作る。
2. ticker ごとの pair delta / excess の中央値を ticker 間で中央値にし、positive share は ticker median pair delta `> 0` の比率とする。
3. trap rate は ticker ごとの cohort trap 率の算術平均とする。

副読みは cohort 等重みで、各 cohort の top5 / matched set の中央値を 1 票とする。reported / 全損 / 中立の 3 case を両重みで全て報告する。`unique_qarp_tickers`、`unique_pair_tuples`、`max_qarp_ticker_share` を各 window へ出す。max share は QARP row 総数に対する最多 ticker の出現比率で、月次反復を独立標本と誤認しないための集中診断である。

## 7. sufficiency gate

各 window は次をすべて満たす場合だけ effect 判定へ進む。

- `eligible_matured_cohorts >= 8`
- quality-passed かつ residual を持つ行の population 比率: cohort median `>= 0.10`
- `candidate_available_cohorts / eligible_matured_cohorts >= 0.75`
- `matched_cohorts / candidate_available_cohorts >= 0.75`
- reported resolved pair rows / membership pair rows `>= 0.75`
- reported pair rows `>= 32`
- `unique_qarp_tickers >= 12`（`3y_postcovid` は `>= 10`）
- `unique_pair_tuples >= 16`
- `max_qarp_ticker_share <= 0.15`
- cohort integrity は既存 evaluation payload の `integrity_status` と `metric_statuses` から導出し、対象 horizon が eligible である。辞書リテラルで status を生成しない。

全損 / 中立代入で coverage、n、unique floor を通さない。coverage が floor 未満でも gate や sector minimum を変更しない。

## 8. effect と trap 非劣後

各 sufficient window の reported / 全損 / 中立、ticker 等重み / cohort 等重みの全 case で次を確認する。

- QARP median annualized excess `> 0`
- matched pair delta median `>= 0`
- QARP trap rate `<= matched core trap rate + 0.02`
- QARP trap rate `<= core top-20 trap rate + 0.02`

`adopt` の effect floor は、上に加えて全 case で次を要求する。

- matched pair delta median `>= +0.02`
- pair delta positive share `>= 0.55`

丸め前の値で判定する。有意性や causal effect は主張しない。cohort/ticker、reported/sensitivity の一部だけを選ばない。

## 9. verdict precedence

語彙は `adopt / shadow_only / insufficient / negative` の 4 つだけとし、まず window ごとに判定してから全体へ畳む。

1. integrity、coverage、maturity、candidate、match、resolved pair、unique ticker、concentration のいずれかが §7 を外せば、その window は `insufficient`。
2. sufficient だが §8 前半の方向または trap 非劣後を 1 case でも外せば、その window は `negative`。
3. §8 前半を全 case で満たすが adopt の effect floor を 1 case でも外せば、その window は `shadow_only`。
4. §7 と §8 の全条件を満たす window だけ `adopt`。

全体は最も弱い window を採る。ただし `insufficient` と `negative` が混在するときは `negative`（観測可能な反証を隠さない）、`insufficient` だけが弱い場合は `insufficient`。全 window が `adopt` のときだけ全体 `adopt`。`adopt` と `shadow_only` の混在は `shadow_only`。

## 10. artifact、独立検算、誠実性

evaluation artifact は全 membership、match identity、return status、case、window 集計、window verdict、overall verdict を持つ YAML とし、report に SHA-256 を固定する。代表 1y / 3y cohort を各 1 件、評価 module を通さず panel / forward CSV から membership、match、population median、pair delta、trap を再計算する。

report の「誠実性の限定」には、重なり窓、price-only、entry regime、COVID 依存、PIT 730 日窓、sector 5 行 floor、greedy match、月次 ticker 反復、事前 diagnostic が非盲検であることを記す。

## 11. production authority と cleanup

本 study は E[r]、FV、rank、gate、selection payload、application DB、market store を変更しない。raw profitability level は calibration panel にだけ残す。

- `adopt` / `shadow_only`: production へ配線せず、exact membership と表示・queue 契約を決める follow-up issue を 1 件だけ起票する。
- `negative`: QARP 専用 evaluator、authority entry、専用 test を通常 tree から削除し、preregistration、report、再現 artifact の hash、raw panel level 列だけを残す。
- `insufficient`: 不足が未満期 cohort だけなら再評価に必要な最小 evaluator を残す。coverage、match、unique ticker、concentration等の構造不足なら `negative` と同じ cleanup を行う。

別 variant、composer、episode state、lane framework、production queue は本 PR へ入れない。
