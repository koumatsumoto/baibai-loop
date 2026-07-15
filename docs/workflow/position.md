---
title: "Workflow — position"
summary: "人間のbroker結果をledger draftへ反映し、source-bound holding reviewとportfolio outcomeへつなぐ工程。"
doc_type: workflow
status: active
last_reviewed: 2026-07-13
related_docs:
  - "../operations/decision-cycle.md"
  - "../reference/portfolio-ledger.md"
  - "../reference/holding-review.md"
---

# Workflow — position

この工程は、proposalを人間のbroker操作へ渡し、人間が確認して報告した注文結果、cash、holding、reservationをcanonical portfolio ledgerへ記録する。自動発注とbroker状態の推定は行わない。ledgerはrepository内の正本だが、broker会計の完全な複製ではない。

## Proposal and approval

AIの新規注文作業は、packet/reviewに束縛したproposal Issueを作り、人間の`approve / defer / reject`を待つところまで。approve後もAIは発注せず、ledgerを変更しない。人間がbrokerを操作し、結果を報告する。

## Human result input

| report | required input | draft event | no-op | stop / ask |
| --- | --- | --- | --- | --- |
| `open` | ticker、quantity、limit、expiry、sector、proposal/approval URL | reservation | 同一reportの全payload一致 | proposal不明、期限/数量/価格不足 |
| `filled` | ticker、quantity、price、executed_at、proposal/approval URL | execution。必要なら先行reservation | 同一event | reservationなしでapproval/guard/expiry/sector不足、未来日時、残数量超過 |
| `cancelled` | reservation_id、cancelled_at、proposal/approval URL | remaining release | 既にterminal | reservation不明、未来日時 |
| `expired` | reservation_id、expired_at、proposal/approval URL | remaining release (`reason=expired`) | 同一event | reservation_id省略、reservation不明、expiry前、未来日時 |

報告がなければ何も更新しない。約定価格と時刻をAIが推定しない。`reservation_id / order_id / event_id`はrepository内のstable identityであり、brokerがIDを報告しない場合はhuman reportから決定的に生成してよい。これはbroker order IDを観測したという意味ではない。

## Ledger draft lifecycle

1. **Draft**: `record-result`がcanonical input hashを固定し、patched local YAMLを生成する。
2. **Inspect**: event、decision reference、cash、reserved cash、quantity、holding差分を読む。
3. **Validate**: schema、event順序、future timestamp、reservation/execution/release、reconciliationを確認する。
4. **Apply**: operation Issueにhash、event、snapshot差分を残し、人間が確認した場合だけsource hashを再照合してcanonicalへcopyし、validationと最終diffを確認する。

CLIはcanonical ledgerを直接上書きしない。詳細recipeは[`operations/decision-cycle.md#human-result-path`](../operations/decision-cycle.md#human-result-path)を正本とする。

## Reservation behavior

- `open`: 同一event identityかつ全payload一致だけをno-opにする。同じ形の別proposalは別reportとして扱う。
- `filled`: active reservationをquantity分消費する。部分約定ならremaining reservationを維持する。
- reservationなしの`filled`: 人間がapproval時刻、guard、expiry、sectorを報告した場合だけreservation→executionを同じdraftへ作る。
- `cancelled`: 指定reservationのremaining quantityをreleaseする。
- `expired`: 人間が未約定の期限到来を確認した場合だけ、指定reservationのremaining quantityをreleaseする。`occurred_at >= expires_at`を必須とする。
- expiryは時刻やbroker状態から自動推定しない。人間報告または明示された運用入力を使う。

## Expired limit outcome

canonical ledgerにhuman-confirmed `release(reason=expired)`があるreservationは、`uv run python -m tools.limit_outcome`で個票の機会観測を作れる。これはread-onlyの補助観測であり、ledger eventやbroker resultを生成しない。

- touch windowはJPX営業sessionで数え、submission日はintraday順序不明のためtouch判定から除外する。corporate-action basis確認にはsubmission日を含める。
- expiry日は注文が15:30 JSTまで有効な場合だけraw/unadjusted daily lowを使う。
- daily lowが指値以下であることはtouchの観測であり、fillの証拠ではない。
- 期限後観測はexpiry直前営業sessionのraw closeから指定horizon末日のraw closeまでの価格変化であり、limit fillやmissed profitを仮定しない。
- partial fillは未約定残数だけを対象とする。
- human-confirmed releaseなし、期限後session不足、raw price欠損、calendar不整合、corporate action、`adjustment_factor`未確認を推定やadjusted priceで埋めない。

結果はledger hashと、同一read-only SQLite transactionで使用したcalendar/raw bar rowsの決定論的fingerprintに束縛したstdout YAMLだけで、canonical/recordsへ保存しない。SQLite file全体のbyte hashには束縛しない。YAMLはoperation Issueへ貼る初期サンプルであり、schemaやaggregateを持たない。反復利用と効果を確認してからstable surfaceへの昇格を判断する。詳細commandは[`operations/decision-cycle.md#expired-limit-feedback`](../operations/decision-cycle.md#expired-limit-feedback)を正本とする。

## Holding review trigger

決算、業績修正、資本政策、永久損失兆候、FV到達、より良い代替候補がmaterialなとき、dated task と一次 IR で event を確認して対象tickerだけreviewする。JPX の予定日は事実入力であり、通知や review の自動起動ではない。全portfolioやscreeningを自動で始めない。

input:

| source | check |
| --- | --- |
| current decision packet + review | as-of、ready、hash、source freshness |
| canonical ledger position | ticker、quantity、cost、market close、event lineage |
| new primary source | packet以後のmaterial deltaだけ |
| replacement candidate | 税・費用控除後の期待値比較 |

最初に`market-price-draft`で指定した最新完全営業日のJ-Quants raw closeを全open holdingについて取得する。1銘柄でも同日raw close、calendar coverage、current SQLite schemaが欠ければ停止し、`adjustment_close`へ代替しない。commandはcanonical ledgerを変更せず、source hashと観測row fingerprintに束縛した新規draftだけを作る。人間が全ticker、日付、raw basis、差分を確認した後だけcanonical ledgerへcopyする。

次に`holding-prepare`でcanonical ledgerの対象open holdingを1銘柄固定workspaceへ接続し、packet/reviewを更新する。`holding-review-build`はpacketの隣接independent reviewを読み、ledgerとcurrent packetからload-bearing scalarを生成する。価格日はledger event時刻ではなく、holdingの最新完全営業日market-price observationとpacketのraw/unadjusted closeを照合する。source値を手入力で変更しない。生成後に`holding-review --root . --input ...`でsource hashとscalar再構築を検証し、人間確認後だけcanonicalへcopyしてvalidationを通す。

## Holding action

| action | meaning |
| --- | --- |
| `hold` | thesis intactで税引後代替が明確に優れない |
| `add` | thesis intact、永久損失acceptable、現値が上限内で追加価値がある |
| `reduce` | thesis at risk、集中超過、または税引後代替が優れる |
| `exit` | thesis brokenまたは確認済み永久損失が優先される |

含み損だけでは売らない。FV到達はreview triggerであり自動exitではない。

## Portfolio outcome

portfolio outcomeはcanonical ledger eventをJPX営業日closeまで再生し、同期間の配当込みTOPIX観測と比較する。内部cash flowをneutralizeし、tax/costを含める。source、period、corporate action、benchmark不足は`unresolved`で、独自推定を作らない。

outcomeは長期判断のcalibration evidenceであり、短期screenの最適化やtrack record主張には使わない。

## Failure / stop conditions

- 人間報告、proposal/approval URL、required fieldがない。
- draft作成後にcanonical ledger hashが変わった。
- eventがfuture-dated、時系列不正、reservationと矛盾する。
- holding packet/reviewがmissing、stale、hash mismatch。
- market closeまたはcorporate actionがunresolved。
- 全open holdingの指定日raw closeまたはmarket calendar coverageが揃わない。

## Validation

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-position ledger
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target ledger
UV_CACHE_DIR=/tmp/uv-cache uv run baibai-loop-validation --target holding-review
```

## Related

- [`../operations/decision-cycle.md`](../operations/decision-cycle.md)
- [`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)
- [`../reference/holding-review.md`](../reference/holding-review.md)
- [`../reference/estimate-calibration.md`](../reference/estimate-calibration.md)
