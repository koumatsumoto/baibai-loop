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

この文書は資本とpositionの運用方針を定める。個別銘柄のFV、entry、limit、exitはthesis、proposal、holding reviewで判断する。機械contractはversioned config、engine model、DB constraintを正本とし、数値fieldをここへ網羅転記しない。

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

上限側の数値は機械にとって充填目標である。提案数量は`floor(1回あたり上限 / 1単元notional)`単元で、渡した金額まで埋まる。1単元が上限を超えれば`budget_guide_exceeded`、notionalが下限を割れば`budget_guide_under`のwarningが付き、いずれも発注を止めない。したがってこの数値を上げることは候補数を増やすことではなく1銘柄あたりの金額を増やすことであり、[Reservation and warnings](#reservation-and-warnings)のticker集中線に先に当たる。

## Starter band

境界にある判断へ、縮小 lot と bucket 上限つきで入る経路。thesis が `judgment.position_intent: starter` を宣言したときだけ開く。帯が許す形は 2 つで、どちらも「full なら許されない何か」と金額の有界化を交換する。

- **(a) 水準を下げる形** — 要求 5 年 base CAGR が full の水準に届かない境界帯（floor 以上 ceiling 未満）。
- **(b) 確証を待たない形** — 要求は full の水準（ceiling 以上）を満たすが、evidence に不完全な軸（`unknown` / 未検証 / 一次 source 欠落 — buy gate が override を要求する例外集合と同じ定義）が残っている。全額の確信を宣言できない lane を、二値の見送りに落とさず縮小 lot で建てる。

| 条件 | 値 |
| --- | --- |
| 要求 5 年 base CAGR | (a) `starter_band.required_return_floor_pct` 以上 `required_return_ceiling_pct` 未満、**または** (b) `required_return_ceiling_pct` 以上かつ evidence 例外軸が残る。evidence が完全で ceiling 以上なら starter は取れない（確信のある判断を縮小 lot へ退避させない） |
| 永久損失結論 | `acceptable` または `unknown`（`elevated` は不可） |
| sizing_action | `reduced` |
| dated catalyst | `judgment.starter_catalyst_date` 必須。再評価を発火させる日付 |
| 1 注文の想定約定額 | `starter_band.max_order_notional_yen` 以下。1 単元がこれを超える銘柄は `starter_lot_exceeds_notional_cap` で defer する |
| starter 合計 | 総資本に対する `starter_band.max_bucket_pct` 以下。これは warning ではなく proposal を止める |

数値は `engine/src/baibai_engine/position/policy.py` の `starter_band` が正本で、[Capital guidance](#capital-guidance) の目安とは別に効く。

永久損失 7 軸、独立レビュー、`approved_by: human` の evidence override は starter でも一切緩めない。緩めるのは要求利回りだけで、その代わりに 1 件あたりの金額と経路全体の資本を有界にする。

撤退基準は 2 つ。**(a)** starter 銘柄に検証済みの永久損失兆候が出たら、FV 到達を待たずに holding review を起こす。**(b)** 1 年経過時点で starter cohort の中央超過が full cohort を下回っていたら、新規 starter を停止する。

(b) の計測経路は proposal から取る。`proposal.payload` に `position_intent` と `starter_catalyst_date` が入り、`ledger_event.proposal_id` が約定を結ぶので、starter で建てた entry 日・価格・数量と再評価の起点日は SQL で引ける。母数が 1 桁のうちは中央超過を算出せず、cohort を並べるだけにする（少数標本で効果量を主張しない）。

各 starter は `starter_catalyst_date` を期日にした follow-up task を持つ。期日に thesis の前提が成立したかを確認しないまま保有を続けない — 帯を開く代償は縮小 lot だけでなく、再評価の義務でもある。

この帯を開く根拠は、機械 E[r] 上位群が母集団を上回る一方、正規化と据え置き倍率を積んだ research の base が要求利回りに届かず全件棄却になっていたという計測である（[診断](../reports/studies/2026-08-06-bargain-capture-diagnosis/report.md)）。形 (b) を足した根拠は、その後の再点検（[厳格性再点検](../reports/studies/2026-08-20-strictness-recheck/report.md)）で、実現した機会費用が「base は水準を満たすのに evidence・確信の不足で二値の見送りに落ちた lane」に集中し、形 (a) だけの帯には流量が来ず空転していたという計測である。ただし cohort 一致数は最小群サイズの閾値に依存し、5y の entry は 2020 年の暴落局面へ強く偏る。**この帯は「機械が確実に勝つ」という前提の上には立っていない。** 狙いは購入件数でも期待値の最大化でもなく、境界帯の実現結果を観測ゼロから非ゼロにすることであり、外れたときの損失を有界に保つのが上限と撤退基準の役割である。

帯の下限・1 注文上限・bucket 上限の数値は計測から導出したものではなく、オーナーが受け入れる損失の大きさから決めた判断である。計測が変わっても自動では動かない。

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

application DBのcanonical ledgerはrepository運用で確認済みのcash、holding、reservation、execution、releaseを表す。broker残高を自動取得・推定・完全照合するものではない。

| human report | ledger action |
| --- | --- |
| `open` | reservation draft。既存同一ならno-op |
| `filled` | execution draft。reservationなしはapproval/guard/expiryを追加確認 |
| `cancelled` | remaining reservation release draft |
| no report | no change |

`record-result`はcanonicalを直接書き換えず、current append headに束縛したlocal draftを作る。差分を確認し、人間確認後の`apply-draft --confirmed`でtransaction内再検証して反映する。精密なbroker会計、二重注文検出、注文監視を投資判断より優先しない。

## Reservation and warnings

reservationは`quantity * price_guard`をcashから引き当て、partial fill後はremaining quantityだけを残す。cancel/expireはrelease eventで明示する。これはrepository snapshotを再計算するための最小状態で、自動broker lifecycleではない。

cash、ticker/sector/common-factor concentration、dry powderはwarning。warningは人間判断を禁止せず、投資価値rankを変更しない。受け入れる場合のoverride contractはledger model/referenceを正本とする。

## Parallel research and serial capital reservation

人間がprimary-research setを複数選んだ場合、企業別researchと独立reviewはticker別laneで並行できる。並行調査は候補比較の時間を短縮するためのもので、資本を先回りして複数銘柄へ予約する許可ではない。

proposalとreservationは投資価値rank順に1件ずつ進める。各`plan-limit`はcurrent DB snapshotへ束縛し、人間の注文結果と必要なledger更新を完了してから次の候補を最新ledgerで再計算する。同じ更新前snapshotから複数proposalを作らないため、先行注文のreserved cashとconcentrationが後続proposalのwarningへ反映される。

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

短期成績、単一銘柄、少数回の注文結果だけでpolicyを変えない。entry estimate、holding/outcome、long-horizon calibrationを突き合わせ、方法変更は[較正の運用契約](./reference/estimate-calibration.md)で事前登録して評価する。

## Policy source

- 思想と優先順位: [`doctrine.md`](./doctrine.md)
- e2e運用: [`AGENTS.md`](../AGENTS.md) の trigger → skill 表
- ledger式とerror/warning: [`reference/portfolio-ledger.md`](./reference/portfolio-ledger.md)
- holding action: [`reference/holding-review.md`](./reference/holding-review.md)
- 機械的なcap/lot/warning値: `engine/src/baibai_engine/position/policy.py`とwrite-time validation
