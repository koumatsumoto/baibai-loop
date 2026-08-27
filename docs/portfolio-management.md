---
title: "Portfolio management"
summary: "投資価値rankingを先に行い、資金目安、human-confirmed ledger、保有規律、年次評価を運用する方針。"
doc_type: governance
status: active
related_docs:
  - "./doctrine.md"
  - "../AGENTS.md"
  - "./reference/portfolio-ledger.md"
  - "./reference/holding-review.md"
---

# Portfolio management

この文書は、資本とpositionの運用方針を定める。個別銘柄のFV、entry、limit、exitは、thesis、proposal、holding reviewが所有する。versioned config、engine model、DB constraintが機械契約を所有するため、数値fieldはここへ網羅転記しない。

## 目的と人間境界

最優先は予算消化ではなく、永久的資本毀損を抑え、一時的にFVとの乖離が大きい候補を拾うこと。候補比較は永久損失、5年期待総合return/FV乖離、portfolioへの追加価値、購入可能性の順。

AIは候補、risk、price、quantity、warningを提案する。人間は`approve / defer / reject`とbroker操作を行う。AIは、人間から報告されていない注文状態を推定しない。

<a id="capital-guidance"></a>

## 資金目安

月40万円は通常の追加資金、1回20〜30万円は指値数量を考えるplanning baselineである。hard capではない。

- 最良候補の1単元が30万円を超えても次点へ自動降格しない。
- quantity、notional、目安超過額、available cash、concentration warningを人間へ示す。
- 目安未満でも数量を無理に増やさない。
- 買う価値がない場合はcashに残す。
- 月次入金triggerだけでscreeningや購入を強制しない。

機械は1回あたりの上限を数量計算の充填目標として使う。提案数量は`floor(1回あたり上限 / 1単元notional)`単元である。1単元が上限を超える場合は`budget_guide_exceeded`、notionalが下限を割る場合は`budget_guide_under`を出すが、どちらも発注を止めない。この上限を増やしても候補数は増えず、1銘柄あたりの金額が増えるため、[予約とwarning](#reservation-and-warnings)のticker集中線へ先に達する。

<a id="starter-band"></a>

## Starter band

Starter bandは、境界にある判断へ縮小lotとbucket上限つきで入る経路である。thesisが`judgment.position_intent: starter`を宣言した場合だけ使える。

### 適用条件

要求5年base CAGRとevidenceは、表の(a)または(b)を満たさなければならない。さらに、残るすべての行を同時に満たす場合だけstarterを使える。

| 条件 | 値 |
| --- | --- |
| 要求5年base CAGRとevidence | **(a) 水準を下げる形:** `starter_band.required_return_floor_pct`以上`required_return_ceiling_pct`未満。**または (b) 確証を待たない形:** `required_return_ceiling_pct`以上で、buy gateがoverrideを要求するevidence例外軸（`unknown`、未検証、一次source欠落）が残る。evidenceが完全でceiling以上ならstarterは取れない |
| 永久損失結論 | `acceptable` または `unknown`（`elevated` は不可） |
| sizing_action | `reduced` |
| dated catalyst | `judgment.starter_catalyst_date` 必須。再評価を発火させる日付 |
| 1 注文の想定約定額 | `starter_band.max_order_notional_yen` 以下。1 単元がこれを超える銘柄は `starter_lot_exceeds_notional_cap` で defer する |
| starter 合計 | 総資本に対する `starter_band.max_bucket_pct` 以下。これは warning ではなく proposal を止める |

数値は`engine/src/baibai_engine/position/policy.py`の`starter_band`が所有し、[資金目安](#capital-guidance)とは別に適用する。

### 緩めない関門

永久損失7軸、独立レビュー、`approved_by: human`のevidence overrideは、starterでも緩めない。緩めるのは要求利回りだけで、その代わりに1件あたりの金額と経路全体の資本を有界にする。

### 停止と再評価

次のいずれかに該当した場合は停止または再評価する。

- starter銘柄に検証済みの永久損失兆候が出た場合は、FV到達を待たずにholding reviewを起こす。
- 1年経過時点でstarter cohortの中央超過がfull cohortを下回った場合は、新規starterを停止する。
- 各starterは`starter_catalyst_date`を期日にしたfollow-up taskを持つ。期日にthesisの前提を再評価せず、保有を続けてはいけない。

cohortの計測はproposalから行う。`proposal.payload`の`position_intent`と`starter_catalyst_date`を、`ledger_event.proposal_id`が約定へ結ぶため、starterのentry日、価格、数量、再評価の起点日はSQLで取得できる。母数が1桁の間は中央超過を算出せず、cohortを並べるだけにする。少数標本で効果量を主張してはいけない。

### 根拠と限界

形(a)を開く根拠は、機械E[r]上位群が母集団を上回る一方、正規化と据え置き倍率を重ねたresearchのbaseが要求利回りに届かず、全件棄却されていたという[診断](../reports/studies/2026-08-06-bargain-capture-diagnosis/report.md)である。形(b)は、baseが水準を満たしてもevidenceまたは確信の不足で見送りとなったlaneに機会費用が集中し、形(a)だけでは流量がなかったという[再点検](../reports/studies/2026-08-20-strictness-recheck/report.md)に基づく。

ただし、cohort一致数は最小群サイズの閾値に依存し、5yのentryは2020年の暴落局面へ強く偏る。この帯は、機械が確実に勝つという前提に立たない。狙いは購入件数や期待値の最大化ではなく、境界帯の実現結果を観測できる状態にし、外れた場合の損失を上限と撤退基準で有界にすることである。

帯の下限、1注文上限、bucket上限は計測から導出した値ではない。オーナーが受け入れる損失の大きさから決めた判断であり、計測が変わっても自動では変えない。

## 投資価値と購入可能性

投資価値rankを決めてから購入可能性を確認する。holdingsとactive reservationsは`held / reserved / held_and_reserved / unheld`としてannotationする。

| input | role |
| --- | --- |
| 永久損失、5年CAGR、FV | rankingの主要判断 |
| portfolio marginal value | 同等候補の追加価値 |
| held/reserved | 買増しと既存proposalのrelation |
| cash/dry powder/concentration | 人間へ示すwarning |
| 20〜30万円 | board lot quantityの目安 |

保有済み、予約中、予算外だけでscreening/research前に候補をhard除外しない。

## 人間が確認したportfolio state

application DBのcanonical ledgerはrepository運用で確認済みのcash、holding、reservation、execution、releaseを表す。broker残高を自動取得・推定・完全照合するものではない。

| human report | ledger action |
| --- | --- |
| `open` | reservation draft。既存同一ならno-op |
| `filled` | execution draft。reservationなしはapproval/guard/expiryを追加確認 |
| `cancelled` | remaining reservation release draft |
| no report | no change |

`record-result`はcanonicalを直接書き換えず、current append headに束縛したlocal draftを作る。差分を確認し、人間確認後の`apply-draft --confirmed`でtransaction内再検証して反映する。精密なbroker会計、二重注文検出、注文監視を投資判断より優先しない。

<a id="reservation-and-warnings"></a>

## 予約とwarning

reservationは`quantity * price_guard`をcashから引き当て、partial fill後はremaining quantityだけを残す。cancel/expireはrelease eventで明示する。これはrepository snapshotを再計算するための最小状態で、自動broker lifecycleではない。

cash、ticker/sector/common-factor concentration、dry powderはwarning。warningは人間判断を禁止せず、投資価値rankを変更しない。受け入れる場合のoverride contractはledger model/referenceを正本とする。

## 並行researchと直列の資本予約

人間がprimary-research setを複数選んだ場合、企業別researchと独立reviewはticker別laneで並行できる。並行調査は候補比較の時間を短縮するためのもので、資本を先回りして複数銘柄へ予約する許可ではない。

proposalとreservationは投資価値rank順に1件ずつ進める。各`plan-limit`はcurrent DB snapshotへ束縛し、人間の注文結果と必要なledger更新を完了してから次の候補を最新ledgerで再計算する。同じ更新前snapshotから複数proposalを作らないため、先行注文のreserved cashとconcentrationが後続proposalのwarningへ反映される。

<a id="holding-discipline"></a>

## 保有規律

株価下落だけでは売らない。購入前に資金繰り、負債返済、cash flow、希薄化、顧客集中、構造衰退、governance/accountingを確認し、長期保有に耐える候補だけを選ぶ。

保有後は決算・material eventで対象tickerだけをreviewする。

- `exit`: thesis brokenまたはverifiedな永久損失が優先。
- `reduce`: thesis at risk、集中超過、税引後で明確に優れる代替。
- `add`: thesis intact、永久損失acceptable、現値が上限内、追加価値あり。
- `hold`: 税引後で勝る代替がなく、thesisが維持される。

FV到達はreview triggerで、自動売却ではない。含み損は単独のexit理由ではない。

## 年次評価

年次にcanonical ledgerの確認済みcash flowと同期間の配当込みTOPIXを同じbasisで比較する。税・費用込みportfolio総合returnを使い、source/期間/corporate action不足は`unresolved`とする。

短期成績、単一銘柄、少数回の注文結果だけでpolicyを変えない。entry estimate、holding/outcome、long-horizon calibrationを突き合わせ、方法変更は[較正の運用契約](./reference/estimate-calibration.md)で事前登録して評価する。

## 正本

- 思想と優先順位: [`doctrine.md`](./doctrine.md)
- e2e運用: [`AGENTS.md`](../AGENTS.md) の trigger → skill 表
- ledger式とerror/warning: [`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md)
- holding action: [`reference/holding-review.md`](./reference/holding-review.md)
- 機械的なcap/lot/warning値: `engine/src/baibai_engine/position/policy.py`とwrite-time validation
