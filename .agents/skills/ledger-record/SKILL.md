---
name: ledger-record
description: 人間が報告した注文結果、資金、income、cost、税、売却約定、年次 outcome を canonical ledger に記録する。推定した broker state は扱わない。
---

# Ledger Record

application DB が portfolio ledger の正本である。人間の報告だけを broker fact とし、期日の経過や reservation の不在から状態を推定しない。event の意味は [`portfolio-ledger.md`](../../../docs/reference/portfolio-ledger.md) を正本とする。

## 共通手順

1. 対応する typed draft command で、current DB に束縛した draft をリポジトリルート配下へ、既存ファイルを上書きせず作成する。
2. 人間が event payload、binding、cash / reservation / holding 差分を確認する。
3. `position apply-draft <draft> --db stores/application/baibai.sqlite --confirmed` で append head と invariant を再検証して適用する。stale の場合は書き込まず、draft を作り直す。

注文結果、資金、年次 outcome は Operation Session を介さず、対応する typed draft command と confirmed apply / publication を直接の human boundary とする。注文結果を無関係な market / macro 不足で止めない。

## 記録対象

- **open / filled / cancelled / expired**: `record-result` を使う。新規 buy は canonical `result=buy` assessment ID と、人間が報告した時刻、数量、価格などが必須である。open は reservation、fill は execution と remaining、terminal report は remaining release を作る。partial fill は remaining がある間だけ継続し、矛盾する report は拒否する。
- **sell**: Position Review 後に `sell-result-draft` を使い、`decision-reference` を review ID に束縛する。market price が stale なら先に price draft を適用する。保有超過 sell は拒否する。
- **資金・income・cost・税**: `position event-draft` で確認した事実ごとに1 event を作る。risk override は `override-draft`、tax estimate は `meta-draft`。入金だけで screening や購入を起動しない。
- **年次 outcome**: ledger を JPX 営業日 close まで再生し、同期間・同 basis の配当込み TOPIX と比較する。`unresolved` は保存せず、不足を解消して再実行する。

CLI option と required field は public `--help`、状態遷移、replay、warning は reference で確認する。例示 command の値を実際の report の代わりに使わない。

```bash
uv run baibai-engine position record-result --db stores/application/baibai.sqlite \
  --decision-reference <ASSESSMENT_ID> --status <STATUS> \
  --occurred-at <ISO8601> --ordered-at <ISO8601> --out <draft>
uv run baibai-engine position sell-result-draft --db stores/application/baibai.sqlite \
  --ticker XXXX --quantity <QTY> --price-yen <PRICE> --occurred-at <ISO8601> \
  --decision-reference <POSITION_REVIEW_ID> --out <draft>
```

## 停止条件

次の場合は停止する。

- 人間報告、buy assessment、required field、decision reference がない
- draft 作成後に append head、assessment、reservation、price / meta row が変わった
- event が future-dated、reservation と矛盾する、または broker 状態が推定である

## 正本

- ledger event と replay: [`portfolio-ledger.md`](../../../docs/reference/portfolio-ledger.md)
- 人間境界と資本規律: [`portfolio-management.md`](../../../docs/portfolio-management.md)
- holding action: [`position-review.md`](../../../docs/reference/position-review.md)
