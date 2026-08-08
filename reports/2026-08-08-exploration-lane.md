---
title: "E[r] longlist 外側の正規化PER探索枠のproduction判断"
summary: "60 cell すべてが effect gate を外し、3y の design と time holdout が符号で割れたため production へ接続しない。"
doc_type: report
status: active
date: 2026-08-08
---

# E[r] longlist 外側の正規化PER探索枠のproduction判断

価値tier: T1 — 現行 E[r] top-20 を維持したまま候補発見の確率を上げられるかを、判断入口へ配線する前に確定させる。

## 結論

**production へ接続しない。** `exploration` lane を selection / shortlist / research へ配線せず、policy 専用 code と authority registry entry と test を通常 tree から削除し、本 report と事前登録 report を反証証跡の正本とする。

事前登録の precedence が返す語は `insufficient` である。ただしこの語は所見を表していない。5 窓中 4 窓は sufficiency を満たしており、**その 4 窓を含む全 5 窓・60 cell が 1 つも effect gate を通らなかった**。sufficiency を落とした 1 窓を仮に満たしたとしても、pair delta median の符号が cell 間で割れるため precedence 2 により `inconclusive` になり、`adoption_candidate` には到達しない。

4 窓の as-of 範囲は固定で、範囲内の cohort は全て満期済みである。新規 cohort は範囲の後ろに付くだけで窓へ入らないので、事前登録が再検定の理由に挙げる「新規満期 cohort」ではこの 4 窓は動かない。残る可能性は、窓の内側で forward coverage が backfill され pair 数と中央値がずれることだけで、それは §「design と time holdout が符号で割れる」の反転を埋めるには足りない。したがって再検定用の surface を残す理由が無い。

## 固定 scope と authority

事前登録 `77e81b2c`、評価実装 `34be1fa1` を用いた。80 panel、`rules_hash=ec8c87c50da68cff` の単一 revision。artifact SHA-256 は `b75197a8a4f532bbfa2553994d75ce0fc1919b4156f4218c8cafa2d6840f5706`。

評価実装の commit は最初の実行より後である。判定へ入る値（coverage 0.75 の 3 条件、paired / changed pair / unique ticker の floor 8、pair delta median `>= +0.03`、positive share `>= 0.60`、trap 非悪化、trap 閾値 `-0.20`、窓と固定 8 cell、precedence）は事前登録から 1 つも動かしていない。最初の実行後に触ったのは 2 点だけで、どちらも gate の入力ではない。

- gate に入らない診断 `changed_only_median` / `changed_only_positive_share` を出力へ足した。
- ticker 等重みの `changed_n` が pair 数のままで、単位数より大きい値を報告していたのを ticker 数へ直した。sufficiency の floor は cohort 等重みだけを読むので判定には入らない。

判定語は変更の前後どちらの実行でも `insufficient` だった。

固定 4 as-of × `3y` / `5y` の 8 cell は `normalized_per_3fy_exploration` を required metric に加えて **8/8 eligible**、`production_change_allowed: true` だった。今回の不採用は authority の不足ではない。

同じ 8 cell の内訳が policy の性格を先に示している。

| as-of | exploration | source rank | comparator (rank 21) |
| --- | --- | ---: | --- |
| 2020-01-31 | 5975 | 24 | 5406 |
| 2020-05-29 | 2768 | 21 | 2768 |
| 2021-01-29 | 8058 | 25 | 7860 |
| 2021-05-31 | 7189 | 21 | 7189 |

封印した 4 as-of のうち 2 つで、policy は単純 top-21 と同じ銘柄を選んでいる。

## 窓別の結果

sufficiency は reported case で判定する。

| window | horizon | 満期 cohort | 候補あり | price pair | total pair | sufficiency | effect |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1y design | 1y | 38 | 35 (92%) | 33 | 32 | pass | fail |
| 1y holdout | 1y | 18 | 18 (100%) | 18 | 16 | pass | fail |
| 3y design | 3y | 21 | 19 (90%) | 17 | 15 | pass | fail |
| 3y holdout | 3y | 19 | 18 (95%) | 14 | 12 | **fail** | fail |
| 5y aggregate | 5y | 21 | 19 (90%) | 16 | 14 | pass | fail |

満期 cohort は、price basis の流動性母集団が親 axis 計測と同じ最小標本 100 行を満たした cohort である。**各窓の as-of 範囲に入る panel 数と満期 cohort 数は 5 窓すべてで一致する**（38 / 18 / 21 / 19 / 21）ので、被覆率の分母が未満期の除外で縮んではいない。

`3y holdout` だけが sufficiency を外した。理由は total basis の unique exploration ticker が 7 で、floor 8 に 1 銘柄足りないことだけである。integrity failure は全窓で 0 件、`entry_price_gap` も 0 件だった。同窓の price 被覆率 0.778 は floor 0.75 を僅差で満たしている。

`price / total` × `reported / 全損 / 中立` × `cohort / ticker 等重み` の 60 cell のうち、**effect gate を通ったのは 0 cell**。

## 事前登録した統計量が 0 に貼りつく理由

pair delta median は 60 cell 中 46 cell で厳密に `0.0000` だった。正だったのは 10 cell、負だったのは 4 cell である。

原因は policy の性質そのものにある。exploration と rank 21 が同じ銘柄になる月では delta が定義上 0 になり、事前登録どおりそれを全 pair の分母へ入れている。

| window | 同一銘柄だった pair（price / reported） |
| --- | --- |
| 1y design | 13 / 33（39%） |
| 1y holdout | 4 / 18（22%） |
| 3y design | 6 / 17（35%） |
| 3y holdout | 3 / 14（21%） |
| 5y aggregate | 5 / 16（31%） |

2〜4 割の月で policy は単純 top-21 と一致する。残る月の勝敗もほぼ拮抗するため、median は 0 を跨げない。これは計測の失敗ではなく **「この policy は 3 割前後の月で comparator と同一であり、残りでも安定して上回らない」という所見そのもの**である。

## design と time holdout が符号で割れる

同一銘柄の月を除いた changed pair だけの中央値は事前登録の predicate ではない。primary の結果を見た後に足した診断であり、gate には入れていない。それでも読む価値があるのは、0 が「差が無い」なのか「そもそも同じ銘柄」なのかを分けられるためである。

| window | price 中央値 | price 勝率 | total 中央値 | total 勝率 |
| --- | ---: | ---: | ---: | ---: |
| 1y design | +0.1336 | 60% | +0.0343 | 63% |
| 1y holdout | +0.0572 | 64% | +0.0584 | 67% |
| 3y design | +0.6764 | 73% | +1.0464 | 80% |
| **3y holdout** | **−0.3729** | **45%** | **−0.3738** | **33%** |
| 5y aggregate | +1.6563 | 55% | +1.9458 | 60% |

最も policy に有利な枠組みで見ても、`3y` の design 窓は +0.68 / +1.05、time holdout は −0.37 / −0.37 で符号が反転する。design と confirm の両方で整合した変更だけを採用するという規律に、この時間分割は明確に反する。

`3y` の全 pair 内訳（total basis, reported）でも同じ形が出る。

| window | positive | zero | negative | median |
| --- | ---: | ---: | ---: | ---: |
| 3y design | 8 | 5 | 2 | +0.1171 |
| 3y holdout | 3 | 3 | 6 | −0.0385 |

## ticker 等重みが 5y の見かけを打ち消す

`5y aggregate` は cohort 等重みでは exploration の median excess +1.80 対 comparator +0.76 と大きく見える。しかし exploration ticker ごとに 1 票へ畳むと、changed pair 中央値は **−0.1583**（勝率 43%）へ反転する。cohort 等重みの数字は同一銘柄の月次反復が作っていた。

`5y` が解決する as-of は 2019-11〜2021-07 の範囲に限られ、`3y design` と同じ as-of 集合である。両者は独立した 2 つの確認ではなく、2020 年の暴落 entry を 2 つの horizon で測った 1 つの事実として読む。

## trap は減らない

事前登録は「exploration の trap rate が rank 21 以下」を要求する。満たさない cell が複数ある。

| cell | exploration | comparator |
| --- | ---: | ---: |
| 3y design price / reported / cohort | 0.118 | 0.000 |
| 3y design total / reported / cohort | 0.067 | 0.000 |
| 1y holdout price / reported / ticker | 0.231 | 0.179 |
| 5y aggregate price / reported / ticker | 0.182 | 0.121 |

効果量が最も良く見える `3y design` は、同時に trap を comparator より増やしている窓でもある。

## 同じ銘柄が繰り返し選ばれる

76 cohort の exploration は 44 銘柄しかなく、`3932` が 7 回、`7189` と `9107` が各 5 回、`5563` が 4 回選ばれている。`3y holdout` の total basis で報告できた 12 pair のうち 5 pair が `3932` で、残りも 6 銘柄しかない。この集中が同窓の unique ticker を 7 まで下げた直接の原因である。

これは operational hypothesis にも直接効く。lane が毎月ほぼ同じ境界銘柄を出すなら、判断入口へ足されるのは「新しい候補」ではなく「先月 reject した同じ銘柄」になりやすい。事前登録で記録済みの membership 事実（exploration ticker の 87% が他 cohort の baseline top-20 にも現れる）と同じ方向を指す。

## 独立検算

2 つの cohort を、評価 module を通さず panel / forward CSV から再計算した。band 内の rank も basis も horizon も異なる組を選んでいる。

`2020-01-31` / `3y` / price basis。

- decile 母集団 937 銘柄、decile size 94、cutoff 8.6974、band metric count 13
- exploration `5975`（rank 24、`normalized_per_3fy=5.677354`）、comparator `5406`
- 母集団 1,276 銘柄、median return +0.003870
- exploration return −0.253696 → excess −0.257566
- comparator return +0.374753 → excess +0.370883

`2021-01-29` / `5y` / total basis。

- decile 母集団 820 銘柄、decile size 82、cutoff 8.060658、band metric count 11
- exploration `8058`（rank 25、`normalized_per_3fy=7.394150`）、comparator `7860`
- 母集団 1,108 銘柄、median return +0.488741
- exploration return +3.965061 → excess +3.476320
- comparator return +0.187451 → excess −0.301290

どちらも評価出力と一致した。後者は exploration 側が母集団中央値を +347pt 上回る大勝ちで、cohort 等重みの median excess を押し上げる型の例である。同じ ticker が複数の月で選ばれるため、こうした 1 銘柄が窓全体の見かけを作る。ticker 等重みを併記する理由がここにある。

## 再現

```bash
.venv/bin/python -m tools.measure_exploration_lane \
  --calibration-dir data/screening/calibration \
  --out exploration-lane.yaml
```

実装は commit `34be1fa1` にある。判定 PR で通常 tree から削除するため、再実行にはその commit を参照する。

## 事前登録の precedence に見つかった穴

precedence 1 は sufficiency 不足を最優先で `insufficient` に落とす。今回はその条件が **1 窓の 1 basis の unique ticker 数が 7 だった** ことだけで成立し、残り 4 窓の決定的な effect 不成立を語彙の上で覆い隠した。

窓別 sufficiency を窓別 verdict へ落としてから全体を畳む方が、同じ規律を保ったまま所見を正しく表す。次に同じ形の事前登録を書くときは、precedence を「窓ごとに判定 → 全窓の語を統合」の 2 段にする。今回の採否は事前登録どおり運用し、この点は後の設計へ回す。

## 残すもの / 消すもの

- 残す: 本 report と事前登録 report、および `normalized_per_3fy` の raw annotation としての既存 production 用途。
- 消す: `selection/exploration.py`、`tools/measure_exploration_lane.py`、`tests/test_exploration_lane.py`、`authority.py` の `normalized_per_3fy_exploration` entry。
- 変えない: E[r]、FV anchor、`normalized_per_3fy` の算出式、liquidity、recommendation、full rank、longlist top-20、shortlist / research の契約。
