---
title: "会社予想改定・複数期加速レーンの事前登録"
summary: "同一期の上方改定と同一fiscal periodのmargin加速を全流動性母集団で順位化し、core外候補の長期増分を固定条件で判定する。"
doc_type: report
status: active
date: 2026-08-10
---

# 会社予想改定・複数期加速レーンの事前登録

価値tier: T1 — 「良くなりつつあるのに価格が追いついていない」という経済仮説が、core E[r] と異なる候補を長期 horizon で安定供給できるかを production 配線前に判定する。

本書は forward outcome を読む前に、PIT 導出、軸、比較、窓、重み、sufficiency、effect、verdict、cleanup を固定する。QARP study #878 から引き継ぐのは事前登録、fail-closed integrity、独立検算の手続きだけで、同 study の結果に合わせて条件を変更しない。結果後に閾値、窓、重み、fallback、variant を変更しない。

## 1. PIT data contract

入力は cohort as-of 以前2,200暦日の `jquants_fin_summaries` だけとし、各 signal の最新観測は as-of から400暦日以内を要求する。future disclosure、非有限値、対象期不明、400日超の値は補完しない。4列は calibration panel だけに保持し、production の financial snapshot、E[r]、FV、rank、gate、selection payloadへ渡さない。

### 1.1 会社予想改定

会社予想は `forecast_profit` だけを使う。`forecast_ordinary_profit` や `forecast_eps` へ fallbackせず、異なる profit basisや株式分割影響を同じ系列へ混ぜない。

target fiscal year end は次のとおり同定する。

- `FY`: 開示行の `fiscal_year_end` を1年進める。本決算時の翌期初出予想であり、その1行を改定と数えない。
- `1Q` / `2Q` / `3Q`: 開示行の `fiscal_year_end` をそのまま使う。
- `4Q` / `5Q`、period不明、fiscal year end不明: 対象外。

最大 target fiscal year end を active target とし、同 target の disclosure date順で2観測以上ある場合だけ次を作る。

| panel field | 定義 | 欠損 |
| --- | --- | --- |
| `forecast_revision_pct_latest` | `(latest forecast_profit - previous forecast_profit) / abs(previous forecast_profit)` | active targetが2観測未満、previousが0、最新観測が400日超 |
| `forecast_revision_streak` | active targetの末尾から、`new > old` が連続する回数。同値・下方改定で0 | active targetが2観測未満、最新観測が400日超 |

赤字予想が `-100 → -80` なら revision は `+0.20` で、損失縮小を上方改定として扱う。targetを跨ぐ `FY 2025 → FY 2026` の初出値同士は比較しない。

SQLite は `(ticker, disclosed_at)` につき1行を保持する。同日訂正は保存された最終行をその日の観測とし、訂正前の版を復元しない。後日の訂正・再開示は disclosure date時点の新しい観測として扱う。sourceが訂正理由を保持しないため、通常改定との意味の違いは主張しない。

### 1.2 複数期加速

`FY` / `1Q` / `2Q` / `3Q` ごとに、同じ `fiscal_period` と1年ずつずらした exact `fiscal_year_end` の3期を要求する。同じ `(fiscal_period, fiscal_year_end)` に複数開示があれば as-of 以前の最新行を使う。各 field は必要値を持つ最新 periodを独立に選ぶが、最新観測が400日を超える場合は欠損とする。決算期変更や3期非連続を別 periodで補わない。

| panel field | 定義 | 欠損 |
| --- | --- | --- |
| `operating_margin_accel_2p` | `OP/Sales_t - 2 × OP/Sales_t-1 + OP/Sales_t-2` | 同一period 3期、operating profit、正のsalesのいずれかが無い |
| `cfo_margin_accel_2p` | `CFO/Sales_t - 2 × CFO/Sales_t-1 + CFO/Sales_t-2` | 同一period 3期、CFO、正のsalesのいずれかが無い |

絶対額の規模差をsignalにしないため、両方をsales比率の二階差にする。negative値もraw factとして保持し、0や中央値へ置換しない。

## 2. 事前 coverage

schema 13の既存 panel identityとmarket summaryだけをread-onlyで結合して測った。forward CSVとrealized returnは読んでいない。`in_population >= 100` の2020-01〜2026-06、78 panelが対象である。

| field / gate | min | median | latest (2026-06-30) | latest n |
| --- | ---: | ---: | ---: | ---: |
| `forecast_revision_pct_latest` | 41.9% | 73.5% | 45.9% | 709 |
| `forecast_revision_streak` | 41.9% | 73.7% | 46.0% | 710 |
| `operating_margin_accel_2p` | 72.2% | 81.0% | 86.1% | 1,328 |
| `cfo_margin_accel_2p` | 70.3% | 84.0% | 88.7% | 1,369 |
| 4列同時 | 35.4% | 53.0% | 37.5% | 578 |
| 4列同時 + 上方改定 gate | 5.9% | 12.6% | 11.4% | 176 |

上方改定 gate後も全78 panelで27銘柄以上あり、top-20とcore外top-5を構成できた。core top-20との重複は72/78 panelで0、残る6 panelも1銘柄だった。exact-sector matchは0〜5、中央値3だったが、match条件やcoverage floorを緩和しない。

## 3. 固定 inflection membership

母集団は `in_population: true`、`population_coverage_status: evaluated` の行である。4 signalがすべて測定済みで、`forecast_revision_pct_latest > 0` かつ `forecast_revision_streak >= 1` の行だけをeligibleとする。会社予想が上向いていない行を、actual accelerationだけでlaneへ入れない。

eligible集合内で4 signalをそれぞれ高い順にmidrank percentile `[0, 1]` へ変換する。同値群には占有順位の平均を与え、ticker順でsignal percentileを分けない。4 percentileの算術平均を `inflection_score` とし、`(inflection_score asc, ticker asc)` の先頭20件を inflection top-20とする。eligibleが20件未満なら `candidate_unavailable`。別score、winsorize、weight、missing平均は試さない。

core top-20は同じpanelの `selection_rank <= 20`。inflection top-20からcore top-20を除いた先頭5件を `inflection_outside_top5` とする。5件未満なら `candidate_unavailable`。

## 4. matched core boundary

各 `inflection_outside_top5` に対し、`selection_rank` 11〜30、`population_coverage_status: evaluated`、正のmarket capを持つ未使用tickerをpoolとする。同じ `sector_33` の中から `abs(ln(market_cap_inflection) - ln(market_cap_core))` が最小の行を選び、同値は `(selection_rank asc, ticker asc)` で解く。inflection score順にgreedy without replacementで1対1 matchする。

同sectorの相手が無い行はunmatchedとし、別sectorやpopulation medianで代用しない。5候補のうち4件以上がmatchしたcohortだけがpair effectを持つ。core top-20全体のtrapも別に出すが、matched comparisonを置き換えない。

## 5. 固定時間窓とhorizon

短期20 / 60 / 120営業日は計算も採否も行わない。PEADの仮説priorを、短期timing signalやcandidate lane採用へ読み替えない。主outcomeは既存forward storeの1y / 3yだけである。

| window | horizon | cohort as-of | 位置づけ |
| --- | --- | --- | --- |
| `1y_design` | 1y | 2020-01-31〜2023-07-31 | retrospective design |
| `1y_time_holdout` | 1y | 2023-08-31〜2025-06-30 | 時系列後半 |
| `3y_all` | 3y | 2020-01-31〜2023-06-30 | 長期aggregate |
| `3y_postcovid` | 3y | 2021-07-30〜2023-06-30 | COVID初期entryを除く感度 |

`3y_postcovid` は `3y_all` の部分集合で独立confirmではない。1y forward窓もcalendar上で重なる。thesis-sourceを主張するには両3y窓のeffect条件通過を必須とし、1yだけの良い結果で代替しない。

## 6. return、欠損、重み

- basisはsplit-adjusted `price_return_only`。各累積returnを `(1 + return)^(1 / horizon_years) - 1` で年率化し、同cohortのresolved `in_population` 年率中央値を引く。
- pair deltaは `inflection annualized excess - matched core annualized excess`。
- trapは累積price excess `< -0.20`。inflection、matched core、core top-20を別々に出す。
- reportedはpair両側がresolvedの行だけを値へ入れる。`unresolved_missing_exit` / `unresolved_stale_exit` は全損 `-1.0` と同cohort resolved population medianの両感度を出す。entry gap、未来horizon、未知status、population median不成立は代入せずintegrity failure。
- 主読みはinflection ticker等重み。各tickerのcohort中央値・率を作ってからticker間を集計する。副読みはcohort等重み。reported / 全損 / 中立を両重みですべて報告する。

## 7. sufficiency gate

各windowは次をすべて満たす場合だけeffect判定へ進む。

- `eligible_matured_cohorts >= 8`
- 上方改定gate通過行 / populationのcohort median `>= 0.10`
- `candidate_available_cohorts / eligible_matured_cohorts >= 0.75`
- `matched_cohorts / candidate_available_cohorts >= 0.75`
- reported resolved pair rows / membership pair rows `>= 0.75`
- reported pair rows `>= 32`
- `unique_inflection_tickers >= 12`（`3y_postcovid` は `>= 10`）
- `unique_pair_tuples >= 16`
- `max_inflection_ticker_share <= 0.15`
- cohort integrityは既存evaluation payloadの `integrity_status` とcore 3 metricの `metric_statuses` がeligible。辞書リテラルでstatusを作らない。

全損 / 中立代入でcoverage、n、unique floorを通さない。事前coverageがfloor付近でもgate、match、unique条件を変更しない。

## 8. effect、trap、verdict

各sufficient windowのreported / 全損 / 中立、ticker等重み / cohort等重みの全caseで次を要求する。

- inflection median annualized excess `> 0`
- matched pair delta median `>= 0`
- inflection trap rate `<= matched core trap rate + 0.02`
- inflection trap rate `<= core top-20 trap rate + 0.02`

`adopt` のeffect floorは、全caseでpair delta median `>= +0.02`、positive share `>= 0.55`。丸め前の値で判定し、有意性やcausal effectは主張しない。

語彙は `adopt / shadow_only / insufficient / negative` の4つだけとする。windowごとに、sufficiency不通過=`insufficient`、方向・trap不通過=`negative`、方向は通るがeffect floor不通過=`shadow_only`、全条件通過=`adopt` とする。全体は最も弱いwindowを採り、`insufficient` と `negative` が混在するときは観測可能な反証を隠さず `negative`。全window `adopt` の場合だけoverall `adopt` とする。

## 9. artifact、独立検算、cleanup

artifactは全membership、match identity、return status、case別個別pairと窓集計、window / overall verdictを持ち、reportへSHA-256を固定する。代表1y / 3y cohortを各1件、評価moduleを通さずpanel / forward CSVからmembership、match、population median、pair delta、trapまで再計算する。

reportの「誠実性の限定」には、重なり窓、price-only、entry regime、COVID依存、2,200日履歴、400日staleness、訂正識別不能、period exact match、CFO/OPの異なる最新period、greedy match、月次ticker反復、非盲検diagnosticを記す。

- `adopt` / `shadow_only`: productionへ配線せず、exact membershipと表示・queue契約を決めるfollow-up issueを1件だけ起票する。短期timing issueは別に作らない。
- `negative`: 専用evaluator/testを削除し、raw panel列、preregistration、report、artifact hashだけを残す。
- `insufficient`: 未満期cohortだけなら最小evaluatorを残す。coverage、candidate、match、unique、concentration等の構造不足なら `negative` と同じcleanupを行う。

application DB、market store、runs storeへwriteせず、calibration storeだけをschema bump後に再構築する。R2へpushしない。
