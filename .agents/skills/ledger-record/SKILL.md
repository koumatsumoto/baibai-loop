---
name: ledger-record
description: 人間が確認した取引・資金の事実を記録し、portfolio outcomeを評価・発行する。
---

# Ledger Record

人間が報告した事実を扱う。event・replay・warningの意味は[Portfolio Ledger](../../../docs/reference/portfolio-ledger.md)が所有する。

## 売買報告の扱い

売買の明示的な報告がなければ、新たな売買変化も未反映の売買もないものとして既存台帳を使う。台帳の最終記録日が古いことや保有レビューの開始を理由に、未反映の売買の有無を再確認しない。報告済みの内容に不足・矛盾がある場合だけ、その点を確認する。

発注報告と約定報告を区別する。発注だけでは保有数量・現金残高を約定後の状態へ変えず、株価の指値到達、期限経過、予約の不在から約定・取消・失効を推定しない。報告済み注文の状態は、次の明示報告まで保持する。

## 記録する操作を選ぶ

| 対象 | 入口 |
| --- | --- |
| open・fill・cancel・expireの報告 | `position broker-fact-draft`。保存済み買付Assessmentを参照する |
| 売却・部分売却の報告 | `position sell-execution-draft`。対応するPosition Reviewを参照する |
| 入出金・income・費用・確定税 | `position event-draft` |
| 人間のrisk override | `position override-draft` |
| 税の見積り設定 | `position meta-draft` |

例:

```bash
uv run baibai-engine position broker-fact-draft --db stores/application/baibai.sqlite \
  --decision-reference <ASSESSMENT_ID> --status <STATUS> \
  --occurred-at <ISO8601> --ordered-at <ISO8601> --out <DRAFT>
```

```bash
uv run baibai-engine position sell-execution-draft --db stores/application/baibai.sqlite \
  --ticker XXXX --quantity <QTY> --price-yen <PRICE> --occurred-at <ISO8601> \
  --decision-reference <POSITION_REVIEW_ID> --out <DRAFT>
```

status等に応じた必須項目は対象commandのpublic `--help`に従う。過去の取引記録を現在の購入適格性で再審査しないが、報告・参照・数量・cashの整合は検証する。入金やincomeにbuy assessmentを要求しない。

## draftとapply

1. 確認した事実からtyped draftを作る。
2. 人間がpayloadとcash・予約・holdingの差分を確認する。
3. `position apply-draft <DRAFT> --db stores/application/baibai.sqlite --confirmed`で適用する。入力状態が変わりstaleになった場合は、draftを作り直して再確認する。

新しいOperationは開始しない。取引事実の保存を無関係なmarket/macro不足で止めず、公開actionから売買数量を推定しない。反映が必要なら[Ops Maintenance](../ops-maintenance/SKILL.md)へ進む。

## Portfolio outcome

outcomeはledger eventではなく、専用commandで評価・発行する。[Historical outcome](../../../docs/reference/portfolio-ledger.md#historical-outcome)に従い、同期間のbenchmarkと税・費用basisを確認する。unresolvedなら正常な結果として発行せず、不足を解消して再評価する。評価可能にするためにledgerの事実を消さない。
