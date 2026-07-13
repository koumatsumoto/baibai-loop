---
title: "Portfolio management"
summary: "投資価値rankingを先に行い、資金目安、human-confirmed ledger、保有規律、年次評価を運用する方針。"
doc_type: governance
status: active
last_reviewed: 2026-07-13
related_docs:
  - "./doctrine.md"
  - "./operations/decision-cycle.md"
  - "./reference/portfolio-ledger.md"
  - "./reference/holding-review.md"
---

# Portfolio management

この文書は資本とpositionの運用方針を定める。個別銘柄のFV、entry、limit、exitはdecision packet、proposal、holding reviewで判断する。機械contractはversioned configとschemaを正本とし、数値fieldをここへ網羅転記しない。

## Purpose and boundary

最優先は予算消化ではなく、永久的資本毀損を抑え、一時的にFVとの乖離が大きい候補を拾うこと。候補比較は永久損失、5年期待総合return/FV乖離、portfolioへの追加価値、購入可能性の順。

AIは候補、risk、price、quantity、warningを提案し、人間がapprove/defer/rejectとbroker操作を行う。AIは人間から報告されていない注文状態を推定しない。

## Capital guidance

月40万円は通常の追加資金、1回20〜30万円は指値数量を考えるplanning baselineである。hard capではない。

- 最良候補の1単元が30万円を超えても次点へ自動降格しない。
- quantity、notional、目安超過額、available cash、concentration warningを人間へ示す。
- 目安未満でも数量を無理に増やさない。
- 買う価値がない場合はcashに残す。
- 月次入金triggerだけでscreeningや購入を強制しない。

## Ranking versus affordability

投資価値rankを決めてから購入可能性を確認する。holdingsとactive reservationsは`held / reserved / held_and_reserved / unheld`としてannotationする。

| input | role |
| --- | --- |
| 永久損失、5年CAGR、FV | rankingの主要判断 |
| portfolio marginal value | 同等候補の追加価値 |
| held/reserved | 買増しと既存proposalのrelation |
| cash/dry powder/concentration | 人間へ示すwarning |
| 20〜30万円 | board lot quantityの目安 |

保有済み、予約中、予算外だけでscreening/research前に候補をhard除外しない。

## Human-confirmed portfolio state

canonical ledgerはrepository内で確認済みのcash、holding、reservation、execution、releaseを表す。broker残高を自動取得・推定・完全照合するものではない。

| human report | ledger action |
| --- | --- |
| `open` | reservation draft。既存同一ならno-op |
| `filled` | execution draft。reservationなしはapproval/guard/expiryを追加確認 |
| `cancelled` | remaining reservation release draft |
| no report | no change |

`record-result`はcanonicalを直接書き換えず、source hash付きlocal draftを作る。差分とvalidationを確認して反映する。精密なbroker会計、二重注文検出、注文監視を投資判断より優先しない。

## Reservation and warnings

reservationは`quantity * price_guard`をcashから引き当て、partial fill後はremaining quantityだけを残す。cancel/expireはrelease eventで明示する。これはrepository snapshotを再計算するための最小状態で、自動broker lifecycleではない。

cash、ticker/sector/common-factor concentration、dry powderはwarning。warningは人間判断を禁止せず、投資価値rankを変更しない。受け入れる場合のoverride contractはledger schema/referenceを正本とする。

## Parallel research and serial capital reservation

人間がprimary-research setを複数選んだ場合、企業別researchと独立reviewはticker別laneで並行できる。並行調査は候補比較の時間を短縮するためのもので、資本を先回りして複数銘柄へ予約する許可ではない。

proposalとreservationは投資価値rank順に1件ずつ進める。各`plan-limit`は出力の`source_ledger_sha256`を現在のcanonical ledgerと照合し、人間の注文結果と必要なledger更新を完了してから次の候補を最新ledgerで再計算する。同じ更新前snapshotから複数proposalを作らないため、先行注文のreserved cashとconcentrationが後続proposalのwarningへ反映される。

## Holding discipline

株価下落だけでは売らない。購入前に資金繰り、負債返済、cash flow、希薄化、顧客集中、構造衰退、governance/accountingを確認し、長期保有に耐える候補だけを選ぶ。

保有後は決算・material eventで対象tickerだけをreviewする。

- `exit`: thesis brokenまたはverifiedな永久損失が優先。
- `reduce`: thesis at risk、集中超過、税引後で明確に優れる代替。
- `add`: thesis intact、永久損失acceptable、現値が上限内、追加価値あり。
- `hold`: 税引後で勝る代替がなく、thesisが維持される。

FV到達はreview triggerで、自動売却ではない。含み損は単独のexit理由ではない。

## Annual evaluation

年次にcanonical ledgerの確認済みcash flowと同期間の配当込みTOPIXを同じbasisで比較する。税・費用込みportfolio総合returnを使い、source/期間/corporate action不足は`unresolved`とする。

短期成績、単一銘柄、少数回の注文結果だけでpolicyを変えない。entry estimate、holding/outcome、long-horizon calibrationを突き合わせ、方法変更はimprovement loopで事前登録して評価する。

## Policy source

- 思想と優先順位: [`doctrine.md`](./doctrine.md)
- e2e運用: [`operations/decision-cycle.md`](./operations/decision-cycle.md)
- ledger式とerror/warning: [`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md)
- holding action: [`reference/holding-review.md`](./reference/holding-review.md)
- 機械的なcap/lot/warning値: `src/baibai_loop/position/policy.py`と対応validator
