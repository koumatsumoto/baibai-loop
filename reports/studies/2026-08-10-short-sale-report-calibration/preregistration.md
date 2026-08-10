---
title: "空売り残高報告の L1 配線と trap 識別 — 事前登録"
summary: "報告なしのゼロ語義、outcome-free sufficiency、固定control、3y/5yの採否条件をforward outcome参照前に固定する。"
doc_type: measurement-record
status: active
date: 2026-08-10
---

# 空売り残高報告の L1 配線と trap 識別 — 事前登録

価値tier: T1 — 機関投資家の報告空売りという独立需給軸で、E[r] 上位に残る永久損失候補の識別力を較正する。

## 1. source と既知情報

J-Quants V2 `get_mkt_short_sale_report`（`/markets/short-sale-report`）は、算出日時点で発行済株式の0.5%以上となり報告された空売り残高を返す。公式仕様は、該当報告のない日にはdataを返さない。API datasetの開始日は2013-11-07、現在の個人向けStandard契約が遡れる期間は10年、Premiumは開始日までと公式リリースに記載される。local契約で実際に取得できた最古日をbackfill reportへ固定し、取得不能な期間を無報告へ補完しない。

本書をcommitするまで、空売り報告軸とforward returnのjoin、effect、trap、窓別verdictを計測しない。API schema、report件数、ticker / reporter / 時価総額分位のcoverageはoutcome-free入力として確認してよい。

## 2. L1 と point-in-time 集約

L1は `disclosed_at`、`calculated_at`、ticker、short seller名、discretionary investment contractor名、investment fund名、残高比率、残高株数、売買単位数、前回算出日・前回比率をtyped rowで保持する。reporter identityは3つの名称を正規化せずtupleのまま用いる。将来時点の名称統合や同一主体推定をしない。

cohort as-ofでは `disclosed_at <= asof` かつ `calculated_at <= asof` のrowだけを読み、同一 `(ticker, reporter identity)` の最新 `calculated_at, disclosed_at` をcurrent stateとする。最新比率が0.5%未満のidentityは報告終了stateとして合計・breadthへ含めない。

panelへ次を保存する。

- `reported_short_ratio`: active identityの残高比率合計。
- `reported_short_breadth`: active identity数。
- `reported_short_latest_disclosed_at`: active identityの最新開示日。active 0件ではas-of以前にcoverage済みの最終開示確認日を保持する。

### ゼロとnull

- source coverageがdataset floorからcohort as-ofまで連続し、active identityが0件なら、ratio / breadthは明示的な0。「0.5%未満または報告不在」を意味し、「空売りが存在しない」とは意味しない。
- dataset floorより前、API plan floorより前、coverage gap、normalization failure、schema不明では3fieldをnullにする。
- 空payloadはcoverage rowが`ok`で記録された場合だけ観測済み無報告として扱う。既存rowを空payloadで置換しない。

## 3. outcome-free sufficiency と主読み

主読みは `reported_short_breadth > 0` の報告あり群と、同一cohortのcoverage済み無報告群の比較とする。報告あり群内のratio順位はH-3診断であり、H-1/H-2の採否を上書きしない。

forwardを読む前に、各horizonのeligible cohortを時間順に前半design / 後半confirmへ分け、次を各窓で要求する。奇数件はconfirmを1件多くする。

1. cohort数8以上。
2. 各cohortの報告あり30 ticker以上、無報告100 ticker以上。
3. 窓全体の報告ありunique ticker 100以上、reporter identity 20以上。
4. market cap quintileごとに報告あり5 ticker以上のcohortが80%以上。
5. 報告ありobservationの単一ticker集中が5%以下、単一reporter identity集中が20%以下。

floorを外した場合、forwardを読まずにcoverage・identity・matched設計だけを修正して再凍結できる。一度でもforwardを読んだ後はfloor、主読み、分割、controlを変更しない。

## 4. 仮説、control、効果条件

H-1は「報告あり群が無報告群より劣後しtrapが多い」（軸方向-1）。各cohortで報告あり1 tickerにつき、次のcontrol cell内から無報告tickerを最大1件、ticker昇順の決定論的tie-breakで対応づける。

固定controlは `market_cap_oku`、`avg_turnover_oku`、`per_trailing`、`dividend_yield`、`close`、`price_change_60d`、`realized_volatility_60d`、`sector_33` の8本。数値controlはcohort内quintile、sectorは同一値。cell最小5 pair、窓のmatched weight 100以上を要求する。

各3y/5y × design/confirm窓で次をすべて満たす場合にH-1を通過とする。

1. raw報告あり−無報告のcohort等重みmedian excessが `<= -0.03`。
2. raw trap deltaが `>= +0.03`。
3. 8 control中7本以上でstratified median excessが負、trap deltaが `>= -0.02`。
4. median excessが負のcohort率2/3以上。

H-2はcontrolへ既存 `margin_short_to_adv` quintileを加えた同じ比較で、matched weight 100以上、median excess `<= -0.03`、trap delta `>= +0.03`、cohort負方向率2/3以上をすべて要求する。H-1が通ってもH-2が外れれば、新しい独立軸としては`negative`とする。

H-3は報告あり群内の `reported_short_ratio` とbreadthのdecile spread、rank IC、相関を診断する。production接続や総合verdictへ使わない。

## 5. verdict とcleanup

窓別語彙は `adoption_candidate` / `negative` / `insufficient` / `inconclusive`。coverage floorを満たさなければ`insufficient`、満たして効果条件を外せば`negative`、全条件通過で`adoption_candidate`とする。全体採用候補は3y/5yのdesign/confirm 4窓すべてでH-1/H-2が通る場合だけ。効果を判定できた窓にnegativeがあれば全体negative、不足窓しかなければinsufficient、それ以外はinconclusive。

採用時もraw annotationだけを候補判断面へ出し、warning、gate、ranking、E[r]、FV、sizingへ接続しない。negative / inconclusiveではpanel列・評価枝・専用testを削除する。L1 tableと再取得可能なsource coverageはfactとして残す。

## 6. 検算

- fixtureでfuture disclosure排除、latest identity選択、0.5%未満終了state、複数identity合計、無報告0、coverage gap nullを固定する。
- 代表cohortのratio / breadth / coverageをpanel builder非依存のSQLで再計算する。
- 代表control cellのpair、median、trapをevaluation module非依存で再計算する。
- gate / ranking / E[r] / FVが不変であることを回帰testで固定する。
