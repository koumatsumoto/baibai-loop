---
name: ledger-record
description: 人間が報告した注文結果、資金、income、cost、税、売却約定、年次 outcome を typed draft、確認、confirmed apply で canonical ledger に記録する。
---

# Ledger Record

application DB が portfolio ledger の正本である。人間の報告だけを broker fact とし、期日経過や reservation 不在から状態を推定しない。event 意味論は [`portfolio-ledger.md`](../../../docs/reference/portfolio-ledger.md) を正本とする。

## 共通 lifecycle

1. 対応する typed draft command で、current DB に束縛した draft を repository root 配下へ exclusive create する。
2. 人間が event payload、binding、cash / reservation / holding 差分を確認する。
3. `position apply-draft <draft> --db stores/application/baibai.sqlite --confirmed` で append head と invariant を再検証して適用する。stale は no-write で draft を作り直す。

session kind は注文結果 `pending-result`、資金 `monthly-contribution`、年次 `annual-outcome`。`pending-result` を無関係な market / macro 不足で止めない。

## 記録の routing

- **proposal decision**: 人間の `approve / defer / reject` を `proposal ... decide` で記録する。approve 時の thesis、price、quantity、expiry、portfolio constraint 不一致は no-write。ledger event が参照済みの proposal を approved 以外へ変えない。
- **open / filled / cancelled / expired**: `record-result` を使う。approved proposal ID と人間報告の時刻・数量・価格等が必須。新規 open と reservation のない fill は current approved proposal を要求する。open は reservation、fill は execution と remaining、terminal report は remaining release を作る。partial fill は remaining がある間だけ継続し、矛盾 report は拒否する。
- **sell**: holding review 後に `sell-result-draft` を使い、`decision-reference` を review ID に束縛する。market price が stale なら先に price draft を適用する。保有超過 sell は拒否する。
- **資金・income・cost・税**: `position event-draft` で確認した事実ごとに1 event を作る。risk override は `override-draft`、tax estimate は `meta-draft`。入金だけで screening や購入を起動しない。
- **年次 outcome**: ledger を JPX 営業日 close まで再生し、同期間・同 basis の配当込み TOPIX と比較する。`unresolved` は保存せず、不足を解消して再実行する。

CLI option と required field は public `--help`、状態遷移・replay・warning は reference を読む。例示 command の値を実 report の代わりに使わない。

```bash
uv run baibai-engine position record-result --db stores/application/baibai.sqlite \
  --proposal-ref <PROPOSAL_ID> --status <STATUS> --occurred-at <ISO8601> --out <draft>
uv run baibai-engine position sell-result-draft --db stores/application/baibai.sqlite \
  --ticker XXXX --quantity <QTY> --price-yen <PRICE> --occurred-at <ISO8601> \
  --decision-reference <HOLDING_REVIEW_ID> --out <draft>
```

## 停止条件

- 人間報告、approved proposal、required field、decision reference がない。
- draft 後に append head、proposal、reservation、price / meta row が変わった。
- future-dated event、reservation 矛盾、推定した broker 状態。

## 正本

- ledger event と replay: [`portfolio-ledger.md`](../../../docs/reference/portfolio-ledger.md)
- 人間境界と資本規律: [`portfolio-management.md`](../../../docs/portfolio-management.md)
- holding action: [`holding-review.md`](../../../docs/reference/holding-review.md)
