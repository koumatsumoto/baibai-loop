---
title: "E[r] 絶対水準と実現 FY 配当の較正 — 事前登録"
summary: "実現 FY 配当の近似 total return、quintile 水準較正、REALIZATION_RATE_ANNUAL の一回推定と 3y/5y 採否契約を計測前に固定する。"
doc_type: measurement-record
status: active
---

# E[r] 絶対水準と実現 FY 配当の較正 — 事前登録

価値tier: T1 — E[r] の予測絶対値を実現 total return と同じ年率座標で検証し、research・保有見直し・cash 判断に使う期待利回りの系統誤差を減らす。

## 0. 既知の観察と計測境界

既存の `er_calibration` は `er_reversion_annual` の相対差を price-only の相対 return と比較する座標であり、E[r] 合計の絶対水準と carry を較正しない。1y の相対 spread 実現率が多くの cohort で 1 を上回ること、entry 時点の market regime ではその差を識別できないこと、3y/5y の core authority gate を共通に通る cohort が 11 件あることは既知である。

本書を commit するまで、実現 FY 配当を forward return に加えた値、`er_level_calibration`、または `REALIZATION_RATE_ANNUAL` の候補値を計測しない。既存 source の schema、`DivAnn` の意味、訂正行と欠損の形、corporate-action の既存正規化契約だけを確認する。

## 1. 実現 FY 配当の固定規則

### 1.1 対象行と窓の端

1 本の forward row について、実際の entry/exit 取引日を `entry_date` / `exit_date` とする。次をすべて満たす financial summary を対象にする。

- `fiscal_period == "FY"`
- `entry_date < fiscal_year_end <= exit_date`
- `disclosed_at` が calibration build 時の評価 cutoff 以下

端の FY は月割りしない。FY 末が左端と同日なら除外し、右端と同日なら全額を含める。年間 DPS を短期窓へ按分する仮定を避けるため、対象 FY が 1 件も観測できない row は配当 0 とせず total-return 座標を未解決にする。

同一 `(ticker, fiscal_year_end)` に複数行がある場合は、cutoff 以下で `dps_actual_annual` が non-null の最新 `disclosed_at` を採る。これは訂正後に利用可能な最新の年間実績値を使う規則であり、結果に応じて初回値と訂正値を選ばない。対象 FY の行は存在するが non-null の `DivAnn` がない場合、または対象 FY 自体を 1 件も観測できない場合は、`realized_dividend_sum` と `total_return` をともに null とし、理由を status に残す。明示された `DivAnn == 0` は観測済み無配として 0 を受理する。負値・非有限値は実現配当として受理しない。

### 1.2 株式基準と式

J-Quants の `DivAnn` は FY 開示時点の 1 株年間 DPS として扱う。各 DPS に、`disclosed_at` より後から forward store の最新 bar 日までの `adjustment_factor` 累積を掛け、`asof_basis_closes()` が entry/exit price に使う最新 bar 基準へ揃える。factor coverage が complete でない row は total-return 座標を未解決にする。

同じ株式基準上で次を保存する。

```text
realized_dividend_sum = Σ normalized DivAnn
realized_dividend_return = realized_dividend_sum / adjusted entry close
total_return = price_return + realized_dividend_return
```

既存の `price_return`、`resolved`、`status` と price-only 評価は変更しない。total-return 側は `realized_dividend_sum`、対象 FY 数、`total_return`、total-return 固有の status / basis を別 field で持つ。forward cache schema は version を上げ、旧 schema を暗黙補完しない。

### 1.3 事前に開示する限界

- `DivAnn` は年間合計であり、中間・期末配当の権利落ち日や受渡日を表さない。FY 末を窓への帰属基準にした近似である。
- FY 末が窓内なら年額を全額含めるため、端の窓では実際の cash-flow timing とずれる。
- source に FY 行自体が無いことと実際の無配を同一視しないが、source から欠落した FY の存在を完全には復元できない。
- 最新 non-null 行を使うため、exit 後に判明した訂正を retrospective outcome に反映する。予測 feature には使わない。
- 株主優待は含めない。`DivAnn` に含まれる特別配当は年間実績の一部として含める。buyback の実現 cash flow は観測しない。
- Premium の配当明細 API は使わない。近似の coverage または誤差が判断不能になる場合だけ、別途費用判断の対象にする。

## 2. `er_level_calibration` の固定規則

既存 `er_calibration` はそのまま残す。新 metric は、`total_return` が resolved で `er_annual` / `er_reversion_annual` / `er_carry_annual` がすべて non-null の流動性母集団を `er_annual` 昇順の 5 quintile に分ける。cohort 内で 100 銘柄未満なら未解決とする。

nominal horizon 年数を `years = horizon_months / 12` とし、各 row の price/total return を次で年率化する。

```text
annualized(r) = (1 + r) ** (1 / years) - 1
```

各 quintile に次の field を出す。

| field | 定義 |
| --- | --- |
| `median_predicted_er_annual` | `er_annual` の中央値 |
| `median_realized_total_return_annual` | row ごとに年率化した `total_return` の中央値 |
| `calibration_error_annual` | realized − predicted |
| `median_predicted_reversion_annual` | `er_reversion_annual` の中央値 |
| `median_predicted_carry_annual` | `er_carry_annual` の中央値 |
| `median_realized_price_return_annual` | row ごとに年率化した `price_return` の中央値 |
| `median_realized_dividend_contribution_annual` | row ごとの annualized(total) − annualized(price) の中央値 |
| `n` | quintile の対象 row 数 |

realized dividend contribution は FY 配当近似による total-price 差であり、予測 carry のうち buyback を直接観測しない。buyback の価格効果は price return に混ざるので、成分表を因果分解として解釈しない。

`er_level_calibration` は optional な既知 metric として authority gate に登録する。全 production 判断へ常時必須化せず、今回の #308 run だけが core 3 metric に加えて明示的に要求する。

## 3. #308 の一回較正契約

### 3.1 required scope

`--run-purpose production_decision`、horizon `3y` / `5y`、required metric は次の 4 つに固定する。

- `recommended_rank_top5`
- `recommended_rank_top10`
- `er_calibration`
- `er_level_calibration`

required asof は、total-return の結果を見る前に core 3 metric で 3y/5y の両方が eligible と判明している次の共通集合 11 件に固定する。

`2019-12-30`, `2020-01-31`, `2020-04-30`, `2020-05-29`, `2020-07-31`, `2020-08-31`, `2020-11-30`, `2021-01-29`, `2021-03-31`, `2021-04-30`, `2021-05-31`

新 metric が未解決になる asof を結果確認後に外さない。1y は同じ 11 asof の診断表を出すが、parameter 判断へ使わない。

### 3.2 3y での候補値

current rate を `r0 = 0.10` とする。3y の required cohort × quintile の各 cell について、§2 の列から次を作る。

```text
U = median_predicted_reversion_annual / r0
Y = median_realized_total_return_annual - median_predicted_carry_annual
raw_rate_3y = Σ(U * Y) / Σ(U ** 2)
candidate_rate = round(raw_rate_3y, 2)
```

切片なしの一回推定とし、重み・loss・quintile 範囲・丸め桁を変えない。分母が 0、raw rate が非有限、候補が `[0.00, 1.00]` 外、または丸め後が 0.10 の場合は変更候補なしとする。

### 3.3 5y 確認と採否

5y cell に同じ式を適用して `raw_rate_5y` を診断値として出す。各 cell の current/candidate prediction と絶対誤差は次で固定する。

```text
predicted_current = carry + r0 * U
predicted_candidate = carry + candidate_rate * U
absolute_error = abs(realized_total - predicted)
```

次をすべて満たす場合だけ、candidate を production 変更候補として別 issue に送る。

1. required scope 全体で authority が `eligible` かつ `production_change_allowed: true`。
2. `(raw_rate_3y - r0)` と `(raw_rate_5y - r0)` が同符号。
3. 3y と 5y の両方で、全 cell の median absolute error が current より **0.01/年（1pt）以上**小さい。
4. cohort ごとの 5 quintile median absolute error が改善する cohort が、3y と 5y のそれぞれで **11 件中 8 件以上**。

1 条件でも欠ければ `REALIZATION_RATE_ANNUAL = 0.10` を維持し、#308 は non-adoption の判断記録で閉じる。全条件を満たしてもこの PR では model parameter を変更しない。候補値で quintile 所属や ranking が変わるため、別 issue で candidate model の再構築・非劣化確認・明示的な code change を行う。

## 4. 実装検証と報告量

- ticker `7203` の合成 fixture で、窓内の複数 FY `DivAnn`、entry price、配当 return、total return を手計算と一致させる。
- 1:2 split を跨ぐ fixture で DPS を factor 0.5 へ正規化し、未正規化値を拒否する。
- FY 行なし、`DivAnn: null`、`DivAnn: 0` を区別し、前 2 つを missing、0 を観測済み無配として扱う。
- 同一 FY の訂正行は最新 non-null だけを採り、重複加算しない。
- price-only の既存出力と `er_calibration` が total-return field の追加だけでは変わらない。
- dated report に source coverage、total-return status 内訳、11 required cohort の 1y/3y/5y quintile 表、authority verdict、3y/5y raw rate、candidate、誤差と cohort 改善数、採否、近似限界を固定する。

この結果は有意性、独立な track record、buyback の因果効果、または regime ごとに異なる parameter を証明しない。月次 cohort は強く重複し、11 asof は COVID 前後へ偏る。#308 で既に観測されなかった entry-regime 依存を、後から別の regime 定義で探索しない。
