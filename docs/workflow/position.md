---
title: "Workflow — position"
summary: "human-confirmed proposal resultをtyped draft / applyでledgerへ反映し、holding reviewとoutcomeへつなぐ工程。"
doc_type: workflow
status: active
last_reviewed: 2026-07-19
---

# Workflow — position

portfolio ledgerの正本はapplication DBである。AIはapproved proposalと人間が報告したbroker factからdraftを作り、人間確認後の明示`apply-draft`だけがDBを変更する。broker状態の推定、自動発注、YAML copyは行わない。

## Proposal boundary

proposalはpacket/reviewとcurrent ledgerから作成し、`pending / approved / deferred / rejected`のcurrent stateを持つ。人間の会話報告だけを`proposal decide`で記録する。新規`open`とactive reservationなしの`filled`はapproved proposalを必須とする。proposalを参照するledger eventができた後、そのproposalをapproved以外へ変更しない。

## Human result

| report | required input | event |
| --- | --- | --- |
| `open` | proposal ID、ticker、quantity、limit、expiry、sector、時刻 | reservation |
| `filled` | proposal ID、ticker、quantity、price、時刻、必要ならreservation ID | execution |
| `cancelled` | proposal ID、reservation ID、時刻 | remaining release |
| `expired` | proposal ID、reservation ID、人間が未約定を確認した時刻 | remaining release (`reason=expired`) |

報告がなければno write。partial fillはremaining reservationを維持し、full fill / cancel / expire後の同一報告はno-change、矛盾報告はhard errorにする。expiryだけから状態を推定しない。

## Draft lifecycle

1. `record-result`またはtyped draft commandがcurrent DBに束縛したephemeral draftをexclusive createする。
2. 人間がevent payload、proposal / reservation、cash、reserved cash、holding quantity、warning差分を確認する。
3. `apply-draft --confirmed`がexpected append head、置換対象price/meta、proposal / reservation binding、domain invariantをtransaction内で再検証する。
4. staleならno-writeとし、current DBからdraftを再生成する。

cash eventは`event-draft`、risk overrideは`override-draft`、tax estimate設定は`meta-draft`、market closeは`market-price-draft`を使う。opening balanceはmigration専用であり、日常CLIから追加しない。

## Event replay

ledgerはaction単位のappend-only eventでcash、reservation、execution、release、income、cost、taxを管理する。late reportも新しいrowとして保存し、`(occurred_at, same_instant_order)`でreplayする。既存event IDを変更せず、訂正は新しいeventで表現する。

## Holding review

決算、material event、永久損失兆候、FV到達、より良い代替候補がmaterialなとき、対象tickerだけreviewする。

1. 最新完全営業日のJ-Quants raw/unadjusted closeを`market-price-draft`で作り、人間確認後にapplyする。
2. current packet/reviewとDB positionをsourceにresearchを更新する。
3. `holding-review-build --db --packet-id`でload-bearing scalarを再構築する。
4. `holding-review`でsource revisionとscalarを検証する。
5. 人間確認後だけ`holding-review publish`でimmutable revisionを保存する。

| action | meaning |
| --- | --- |
| `hold` | thesis intactで税引後代替が明確に優れない |
| `add` | thesis intact、永久損失acceptable、現値が上限内で追加価値がある |
| `reduce` | thesis at risk、集中超過、または税引後代替が優れる |
| `exit` | thesis brokenまたは確認済み永久損失が優先される |

含み損だけでは売らない。FV到達はreview triggerであり自動exitではない。

## Portfolio outcome

portfolio outcomeはDB ledger eventをJPX営業日closeまで再生し、同期間の配当込みTOPIX observationと比較してapplication DBへpublishする。internal cash flowをneutralizeし、tax / costを含める。source、period、corporate action、benchmark不足は`unresolved`にする。

## Stop conditions

- 人間報告、approved proposal、required fieldがない
- draft生成後にappend head、proposal、reservation、price/meta rowが変わった
- eventがfuture-datedまたはreservation stateと矛盾する
- packet revisionまたはholding scalarが一致しない
- raw close、calendar coverage、corporate action basisがunresolved

public commandと詳細recipeは[`../operations/decision-cycle.md`](../operations/decision-cycle.md)、ledger semanticsは[`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)を正本とする。
