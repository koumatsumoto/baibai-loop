---
title: "E[r] longlist 外側の正規化PER探索枠に関する事前登録"
summary: "full rank 21〜40の良好decileから1件を足すexact policyを、単純top-21と固定窓で比較する条件を固定する。"
doc_type: report
status: active
date: 2026-08-08
---

# E[r] longlist 外側の正規化PER探索枠に関する事前登録

価値tier: T1 — 現行 E[r] top-20 を全件維持したまま、その直後の band から複数年利益ベースでも割安な 1 件を人間の判断入口へ足し、既存候補を失わずに投資可能な候補を見つける確率を上げる。

本書は forward outcome を読む前に、exact policy・比較対象・時間窓・return 単位・分母・欠損処理・重み感度・verdict の precedence を固定する。以降の評価はここで固定した条件だけで採点し、同じ artifact 上で閾値を動かして再判定しない。

## 1. 固定 policy

Baseline は cohort as-of を production の `build_selection_payload` へ通して得る full rank とする。較正 panel の `selection_rank` がその full rank であり（`calibration/panel.py` の `_replay_ranks(mode="full_ranking")`）、別実装で rank を近似しない。

1. Recommendations と baseline longlist rank 1〜20 は現行どおり出す。
2. 親 raw-axis 計測と同じ `in_population: true` で、正かつ有限な `normalized_per_3fy` を持つ全流動性母集団を `(normalized_per_3fy asc, ticker asc)` で並べ、先頭 `ceil(n / 10)` 件を良好 decile とする。E[r] null で full rank を持たない行も decile 母集団に含める。同値もこの順序で切り、毎回同じ件数にする。
3. full rank 21〜40 に正かつ有限な `normalized_per_3fy` が 10 件未満なら、その cohort を `insufficient_metric_coverage` として exploration を出さない。
4. rank 21〜40 かつ良好 decile の銘柄を `(full_rank asc, ticker asc)` で並べ、先頭 1 件を exploration とする。該当 0 件なら exploration は 0 件とする。
5. Exploration は元の full rank、`selection_lane`、丸めない `normalized_per_3fy`、良好 decile cutoff、band metric count、policy version を保持する。rank 21 や recommendation と偽装しない。

`normalized_per_3fy: null` は良い値にも悪い値にも補完しない。既存 top-20 からは何も除かない。band 内の絶対最小値を採る policy は、親 evidence が支持していない extreme order statistic であり、3 FY 平均の単年支配を強めるため採らない。

比較対象は **単純 top-21**（full rank 21 をそのまま 1 件足す）とする。

## 2. 固定時間窓と非盲検性

| horizon | retrospective design | retrospective time holdout / aggregate |
| --- | --- | --- |
| 1y | 2019-11-29〜2022-12-30 | 2024-01-31〜2025-06-30 |
| 3y | 2019-11-29〜2021-07-30 | 2022-01-31〜2023-07-31 |
| 5y | — | 2019-11-29〜2021-07-30 の authority eligible 全 cohort |

Production authority は固定 4 as-of `2020-01-31`, `2020-05-29`, `2021-01-29`, `2021-05-31` × `3y, 5y` の 8 cell を使い、良い月へ差し替えない。

Feature は既知の raw-axis outcome を見て選んでいるため、上表の time holdout や固定 8 cell を独立 confirm とは呼ばない。全窓は 1 つの retrospective policy replay であり、3y design と 5y aggregate は同じ as-of 集合を 2 つの horizon で測る入れ子である。独立 evidence は新規満期 cohort と production 接続後の judgment bridge に限る。試す production policy family は §1 の 1 件だけとする。

## 3. Return、pair、欠損の定義

- 全 horizon で年率化せず、既存 calibration と同じ**累積 return** を使う。
- Price basis は split 調整済み price return、total basis は同期間の total return を使う。候補 excess は、候補の累積 return から同じ cohort・basis の resolved liquidity population 累積 return 中央値を引く。
- Pair delta は `exploration excess - rank 21 excess` とする。丸め前の値で判定し、report 表示だけを丸める。
- 同一 ticker が exploration と rank 21 になった pair は delta 0 として全 pair 分母に含め、positive には数えない。全 pair 数と changed pair 数を別に出す。
- Trap は各候補の累積 excess `< -0.20` とする。Trap rate は cohort ごとの 0 / 1 を等重みで平均する。
- Reported case は両候補が同じ basis で resolved な cohort だけを pair にする。ただし policy membership からは落とさず、未解決 status を別表で開示する。
- `unresolved_missing_exit` / `unresolved_stale_exit` は、全損代入では候補 return `-1.0`、中立代入では同じ cohort・basis の母集団中央値を入れる。design、time holdout、5y の全未解決 selected pair へ適用し、固定 8 cell だけで代用しない。
- `entry_price_gap`、未知 status、未満期、population median 不成立、as-of / rules hash 不一致は代入せず integrity failure とする。silent drop は禁止する。

## 4. Cohort 等重みと ticker 等重み

同一 ticker の月次反復を独立証拠とみなさないため、全 effect predicate を次の 2 通りで計算する。

1. **Cohort 等重み**: 各 as-of pair を 1 票とする。
2. **Exploration ticker 等重み**: exploration ticker ごとに pair delta と各候補 excess の cohort median を 1 票にする。Positive share は ticker median delta `> 0` の share とする。Trap は ticker ごとに cohort trap 率を計算し、その算術平均を使う。

各 window / basis で `unique_exploration_tickers`、`unique_pair_tuples`、`max_exploration_ticker_share` を併記する。unique exploration ticker が 8 未満なら effect を判定しない。

## 5. Window の sufficiency gate

各 1y / 3y split と 5y aggregate は次をすべて満たす場合だけ effect 判定へ進む。

- `eligible_matured_cohorts`、`candidate_available_cohorts`、`price_resolved_pairs`、`total_resolved_pairs` を別々に出す。
- `candidate_available_cohorts / eligible_matured_cohorts >= 0.75`。
- `price_resolved_pairs / candidate_available_cohorts >= 0.75`。
- `total_resolved_pairs / price_resolved_pairs >= 0.75`。
- price / total の各 basis で paired n `>= 8`、changed pair n `>= 8`、unique exploration ticker n `>= 8`。

Coverage と n は reported case で判定する。全損 / 中立代入で n を水増しして sufficiency を通さない。

## 6. Effect gate

sufficiency を通った各 window について、price / total、reported / 全損 / 中立、cohort 等重み / ticker 等重みの全組合せで次を満たす場合だけ window pass とする。

- Pair delta median `>= +0.03`。
- Positive share `>= 0.60`。
- Exploration trap rate `<=` rank 21 trap rate。
- Exploration 自身の median excess `> 0`。

1y design / 1y holdout / 3y design / 3y holdout / 5y aggregate の全 window pass を要求する。5y だけ効果量 floor を弱めない。有意性や track record は主張しない。

## 7. Policy authority

raw `normalized_per_3fy` と core 3 metric は入力 axis / ranking 基盤の authority として全固定 cell で eligible を要求する。ただし exact policy の production 権限には使わない。

optional policy metric `normalized_per_3fy_exploration` を別に定義し、固定 8 cell で次を封印する。

- production full rank から同じ policy membership を再生成できる。
- exploration / rank 21 の ticker、source rank、metric 値、良好 decile cutoff、band count が artifact と一致する。
- 両候補の return status、basis、sensitivity が欠損なく列挙される。
- 3y / 5y ごとの aggregate delta 符号が price / total、reported / 全損 / 中立、cohort / ticker 等重みで反転しない。

policy metric が required manifest に無い、`metric_eligible` でない、または core 3 / raw axis の status と不整合なら production へ接続しない。

## 8. Verdict の precedence

1. authority、integrity、candidate availability、coverage、paired n、changed pair n、unique ticker n のどれかが不足したら `insufficient`。
2. sufficiency を満たし、design / holdout、basis、欠損 sensitivity、weighting の間で pair delta median の符号が分かれたら `inconclusive`。
3. 符号は分かれないが、いずれかの effect predicate が基準を満たさなければ `negative`。
4. 全 sufficiency / effect / authority 条件を満たす場合だけ `adoption_candidate`。

同じ artifact で閾値を変更して再判定しない。新規満期 cohort または contract-level の capacity 変更だけを再検定理由にする。

## 9. Forward outcome を読まずに確定した membership 事実

80 個の既存 panel で membership だけを再生した結果を、判定条件の外に置く前提として記録する。

| 観察 | 値 |
| --- | --- |
| rank 21〜40 の正かつ有限な `normalized_per_3fy` 件数 | min 9 / median 15 / max 20 |
| exploration を持つ cohort | 76 / 80（`insufficient_metric_coverage` 2、良好 decile 該当なし 2） |
| exploration が band 内絶対最小と異なる cohort | 46 / 76 |
| exploration が rank 21 と異なる cohort（changed pair） | 58 / 76 |
| top-20 に既に含まれる良好 decile 銘柄数 | median 5.5（min 2 / max 10） |
| unique exploration ticker | 44 / 76 cohort |

窓別の membership 側 floor も満たす。

| window | eligible cohorts | candidate available | unique exploration ticker | changed pair |
| --- | ---: | ---: | ---: | ---: |
| 1y design | 38 | 35 (92%) | 22 | 22 |
| 1y holdout | 18 | 18 (100%) | 13 | 14 |
| 3y design | 21 | 19 (90%) | 13 | 13 |
| 3y holdout | 19 | 18 (95%) | 10 | 15 |
| 5y aggregate | 21 | 19 (90%) | 13 | 13 |

### 事前に判明している recall 主張の弱さ

同じ membership 再生から、この lane の「人間が他では見ない候補を届ける」という主張は限定的だと分かる。判定条件は変えず、結果の解釈に使う。

| 観察 | 値 |
| --- | --- |
| exploration ticker が他の cohort の baseline top-20 にも現れる | 66 / 76（87%） |
| 同 ±3 か月以内 | 49 / 76（64%） |
| 同 前月の top-20 に居た | 20 / 76 |
| exploration が axis 上で top-20 の良好 decile 銘柄すべてより割安 | 14 / 76（18%） |
| exploration が top-20 の良好 decile 銘柄すべてより割高 | 16 / 76（21%） |

したがって本 policy の増分は「未知銘柄の発見」ではなく「境界帯に居て複数年利益でも割安な 1 件を、その月の判断入口へ確実に載せる」ことに限定される。この限定は §6 の effect gate を緩める理由にしない。

## 10. 非採用時の扱い

`negative` / `inconclusive` では policy 専用 evaluator・authority registry entry・test を通常 tree から削除し、結果 report だけを残す。`insufficient` では新規満期 cohort で再判定するための最小 surface だけを残し、production selection / shortlist は変えない。`adoption_candidate` の場合だけ typed `exploration` を production へ接続する。
