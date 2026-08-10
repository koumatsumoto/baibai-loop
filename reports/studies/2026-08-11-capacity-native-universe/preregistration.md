# Capacity-native universe replay preregistration

価値tier: T1 — 実注文容量で投資可能な銘柄へ現行E[r]を広げても、長期after-cost価値と退出可能性が維持されるかを判定する。

## 1. Blindness と固定順序

この事前登録は outcome-free Stage 0 commit `9a819392` の後、forward outcomeを読む前に固定する。Stage 0 が読んだ入力は point-in-time panel、各as-of以前のdaily bars、同日master snapshotだけで、[`stage0.yaml`](./stage0.yaml) は `outcome_data_read: false` と `forward_inputs: []` を持つ。

このcommit以後は membership、notional、参加率、退出日数、window、cost、basis、weighting、sufficiency、effect、verdictを変更しない。不具合修正が結果を変え得る場合は修正内容と影響をreportに開示するが、好ましい結果へ寄せる修正はしない。

## 2. Membership と順位

全policyは同じ cohort の次の行だけを入口にする。

- `pass_screen == true`
- `population_coverage_status == evaluated`
- finiteな現行 `er_annual`
- 現行 `rules_hash: f4a3f3f20838e1cd`
- ordinary-share / market scope / financial required-field contractはproduction panelのまま
- historical JPX regulation flagはpanelに無いため、両policyとも同じ空flag replay contract

順位は `er_annual` 降順、同値はticker昇順。diversity cap、過去候補、macro、supply-demand gateを加えない。E[r]、FV、rank、gate、selection payload、production rulesは変更しない。

### current policy

- `market_cap_oku >= 100`
- `avg_turnover_oku >= 1.0`
- `listing_span_days >= 182`

### capacity policy

Stage 0 の計算契約をそのまま使う。

- trading unit 100株。JPX一次資料では2018-10-01に全国取引所の内国株式が100株へ統一され、最古cohortは2019-11-29（[JPX](https://www.jpx.co.jp/equities/improvements/unit/index.html)）
- 直近60市場session。row欠損・volume/turnover欠損を0円の無出来として含める
- p20はnearest-rank 20%点、補間なし
- `minimum_lot_yen = as-of未調整終値 × 100`
- `capacity_days = max(notional, minimum_lot_yen) / (p20 trading value × 0.01)`
- usable session 57/60以上、nonzero volume share 95%以上、capacity days 5以下、listing span proxy 365日以上
- starter 10万円、primary standard 30万円、stress 50万円を固定し、採否はstandardを使う
- Amihud、zero-return、no-tradeは診断だけでmembership gateにしない

比較policyは次の4つ。

1. `current_core_top20`: current eligibleの上位20
2. `capacity_core_top20`: standard capacity eligibleの上位20
3. `capacity_only_outside_current_top5`: `capacity_core_top20`内でcurrent不通過の上位5
4. `current_boundary_21_25`: current eligibleのrank 21〜25

不足時に下位rank、別sector、別notionalで補完しない。membership件数を分母に unresolved を残す。

## 3. Windows

calendar targetはrepositoryの `HorizonSpec.target_date` を使う。as-of境界は結果後に動かさない。

| window | horizon | as-of範囲 | Stage 0 matured cohort |
| --- | --- | --- | ---: |
| `1y_design` | 1y | 2020-01-01〜2023-07-31 | 43 |
| `1y_time_holdout` | 1y | 2023-08-01〜2025-06-30 | 23 |
| `3y_all` | 3y | 2020-01-01〜2023-06-30 | 42 |
| `3y_postcovid` | 3y | 2021-07-01〜2023-06-30 | 24 |
| `5y_all` | 5y | targetが2026-08-10以前の全cohort（2019-11-29〜2021-07-30） | 21 |

`1y_design` と `1y_time_holdout` が時間分割の主対で、`3y_all` / `3y_postcovid` がproduction authorityの長期方向、`5y_all` が最長期の方向とtrapを確認する。

## 4. Outcome basis とcost

- `price`: split-adjusted price return、`status == resolved`
- `total`: repositoryのFY実績配当込みtotal return、`total_return_status == resolved`
- population excess: 同じ cohort / horizon / basis で解決した `in_population == true` 行のreturn中央値を各rowから引く
- trap: cost控除後population excess `<= -0.20`
- round-trip cost: 50bps、200bps、500bpsを選択policyの累積returnから一律控除する。population中央値へcostを控除しない
- 主読み: 200bps。price / total の両方がsufficientなら両方をgateに使い、良いbasisだけを選ばない
- 50bps / 500bpsは感度。500bpsだけの不通過は自動negativeにせず、cost sensitivityとしてinconclusiveにできる

主weightは解決したticker-as-of観測の等重み。中央値、trap率、positive shareをpoolして計算する。同一tickerの月次反復はStage 0のmax ticker share gateで有界化する。副weightはcohort等重みで、cohortごとの中央値・trap率を先に計算してからwindow内を等重み集計する。両weightを全basis / costで報告し、採否は主weightを使う。

## 5. Integrity とwindow sufficiency

各 cohort の eligibility は `calibration-evaluate` が発行する `cohort_integrity` とrequired metric statusを読む。studyが独自の辞書リテラルでcore eligibilityを発行しない。required metricは `recommended_rank_top5`、`recommended_rank_top10`、`er_calibration`。

windowは次をすべて満たすときだけeffectを判定する。

- eligible matured cohort 8以上
- `capacity_core_top20` 完全availability 90%以上
- `capacity_only_outside_current_top5` 完全availability 75%以上
- `current_core_top20` と `current_boundary_21_25` の完全availabilityが各90%以上
- 各policyの price resolved / membership 75%以上
- 各policyの total resolved / price resolved 75%以上
- capacity-only incremental selectionsのunique ticker: 1y各窓25以上、3y各窓15以上、5y aggregate 8以上
- max ticker share 15%以下
- unknown forward status、重複 `(asof,ticker,horizon)`、panel/forward partition不一致を0件

Stage 0のoutcome-free availability / concentrationは全項目passした。outcome resolutionとcohort integrityはU3で初めて読む。

## 6. Effect gates

以下は200bps控除後、ticker-as-of等重みのprice / total両basisへ適用する。

### Policy non-inferiority

- `capacity_core_top20` median excess − `current_core_top20` median excess `>= -0.02`
- capacity trap rate − current trap rate `<= +0.02`

### Incremental value

- `capacity_only_outside_current_top5` median excess `> 0`
- cohort別の incremental median − boundary median のwindow中央値 `>= 0`
- 上記cohort deltaが正のshare `>= 0.55`
- incremental trap rate − boundary trap rate `<= +0.02`
- incrementalの stale / missing-exit / missing-entry 合算率 − boundary同率 `<= +0.02`

unresolvedは分母から落とさず、`status`別件数、stale exit、missing exit（delisting exit不明を含む）、missing entry、adjustment-factor不足を全policyで報告する。

### E[r] transportability

capacity eligibleかつcurrent不通過の全行をcohort内 `er_annual` quintileへ固定する。同値境界はticker順で安定化し、空quintileを補完しない。次を200bps後のprice / total両basisで要求する。

- Q5 median excess > Q1 median excess
- `E[r] >= 8.5%` median excess > 0
- Q5 trap rate − Q1 trap rate `<= +0.02`
- `1y_design`、`1y_time_holdout`、`3y_all`で同方向

carry / reversion component、anchor、market-cap、liquidity、Amihud、zero-return、capacity-days bucketは交絡診断として報告し、事後gateにしない。

## 7. Window verdict とoverall

各windowを先に4語へ落とす。

- `insufficient`: §5のいずれかを満たさずeffectを判定できない
- `adoption_candidate`: 200bpsのprice / total両basisで§6の全gateを満たす
- `negative`: policy non-inferiorityを2pt超で破る、またはprice / total両basisで incremental median `<= 0`・boundary delta `< 0`・positive share `< 0.55`の3条件をすべて満たす、またはtransportabilityのQ5-Q1方向が両basisで逆転する
- `inconclusive`: sufficientだが上のadoption / negativeのどちらにも収束しない、basisやtime splitで方向が割れる、または500bpsだけで実行価値が消える

overallは次の順で決める。

1. effect判定可能windowが0なら `insufficient`
2. 1y / 3yの4 windowがすべて `adoption_candidate` で、5yもcapacity non-inferiority・incremental方向・trap非劣後・transportability方向を満たす場合だけ `adoption_candidate`
3. adoption方向とnegative方向がwindow間で割れる場合は `inconclusive`
4. 全sufficient 1y / 3y windowが `negative`、または同じ中心gateが1y design/holdoutと3yで一貫してnegativeなら `negative`
5. それ以外は `inconclusive`

有意性・統計的優位は主張せず、重複する月次windowを独立標本と呼ばない。

## 8. Artifact と独立検算

結果artifactは cohort / policy / selected tickerごとに、source rank、E[r]、lot、p20 turnover、capacity days、return status、price / total、cost bracket、population excess、trapを持つ。window集計、transportability、composition、4語verdictを同じartifactへ入れ、reportへSHA-256を固定する。

独立検算はstudy evaluatorをimportせず、標準CSV / SQLite readerから代表cohortを再計算する。

- 1y: 2023-06-30
- 3y: 2022-06-30
- 5y: 2020-06-30
- minimum lot、nearest-rank p20、capacity days、current/capacity membership
- current/capacity top20、incremental top5、boundary 21〜25
- population median、200bps後excess、trap
- capacity-only E[r] quintile membership

3 cohortのticker集合、主要数値、集計が一致しなければverdictを発行しない。

## 9. Cleanup とproduction境界

- `negative` / `inconclusive`: capacity evaluator、専用test、study-local cacheを最終commitで削除し、preregistration、dated report、結果artifact hash、git historyだけを残す
- `insufficient`: 未満期だけが原因なら再計測に必要な最小surfaceを残せる。PIT単元、coverage、availability、concentrationの構造不足なら削除する
- `adoption_candidate`: evaluatorをproductionへ接続せず、selection contract / schema / UI / skill / migrationを設計する別issueだけを起票する

このPRではproduction E[r]、FV、rank、gate、rules、selection payloadを変更しない。
