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

| 対象 | 入口と参照 |
| --- | --- |
| 買付注文・約定・取消・失効 | `position broker-fact-draft`。具体buy条件を公開したInvestment Proposal ID、または下記の直接取引を使う |
| 売付注文・その後続報告 | `position broker-fact-draft --side sell`。注文の参照とreservation IDを固定する |
| 売却・部分売却の約定 | `position sell-execution-draft`。判断参照は任意。既存注文に対応する場合はreservation IDを使う |
| 入出金・income・費用・確定税 | `position event-draft` |
| 人間のrisk override | `position override-draft` |
| 税の見積り設定 | `position meta-draft` |

### 提案に対応する買付注文

人間が確認した実注文のticker・数量・guard・日付を使う。正確な時刻や期限を記録の前提にしない。以下のplaceholderは報告された値へ置換し、提案条件から注文事実を補完しない。

```bash
uv run baibai-engine position broker-fact-draft --db stores/application/baibai.sqlite \
  --decision-reference <INVESTMENT_PROPOSAL_ID> --status open \
  --ticker <TICKER> --quantity <QTY> --price-guard-yen <GUARD> \
  --occurred-on <YYYY-MM-DD> --sector <SECTOR> --out <DRAFT>
```

### 提案なしの実買付

方針外の追加購入も人間確認済みの事実として記録する。上の注文例の`--decision-reference`を`--direct-fact`へ置き換え、tickerを明示する。初回draftが`manual-fact-…`参照を発行し、applyで取引と同時に保存する。後続・再試行は返された参照とreservation/order/execution IDを保持する。同じ報告の再送で新しい参照を作らない。直接取引を記録するためにAssessmentやProposalを作らない。

### 約定と後続報告

注文未登録の実買付は次の入口で記録できる。業種は銘柄情報から確認する。

```bash
uv run baibai-engine position broker-fact-draft --db stores/application/baibai.sqlite \
  --direct-fact --status filled --ticker <TICKER> --quantity <QTY> \
  --price-yen <PRICE> --sector <SECTOR> --occurred-on <YYYY-MM-DD> --out <DRAFT>
```

買付約定は日付・銘柄・数量・単価を基本に記録する。対応する保存注文がある場合だけreservationへ束縛し、注文がなければ発注時刻や期限を聞き直したり架空の注文を作らない。

```bash
uv run baibai-engine position broker-fact-draft --db stores/application/baibai.sqlite \
  --decision-reference <SAVED_REFERENCE> --status filled \
  --reservation-id <RESERVATION_ID> --execution-id <BROKER_EXECUTION_ID> \
  --quantity <FILLED_QTY> --price-yen <FILL_PRICE> \
  --occurred-on <YYYY-MM-DD> --out <DRAFT>
uv run baibai-engine position sell-execution-draft --db stores/application/baibai.sqlite \
  --ticker <TICKER> --quantity <QTY> --price-yen <PRICE> \
  --occurred-on <YYYY-MM-DD> --execution-id <BROKER_EXECUTION_ID> --out <DRAFT>
```

売却が保存注文に対応するなら`--reservation-id`を加える。判断参照を報告できる場合は`--decision-reference`で残すが、新規Position Review artifactを要求しない。売付の未約定注文は`broker-fact-draft --side sell --status open`で記録し、見込代金をcashに入れない。

注文に束縛された約定・取消・失効は同じ参照とreservation IDを使う。約定時刻も報告された場合は`--occurred-on`の代わりに`--occurred-at`を使う。実約定を区別するbroker IDがない場合は推測して作らず、同日別約定を区別できなければ確認する。status等の必須項目は対象commandのpublic `--help`に従う。取引を現在の購入適格性で再審査しないが、報告・参照・数量・cashの整合は検証する。入金やincomeに買付提案を要求しない。

費用も`event-draft --occurred-on <YYYY-MM-DD>`で日付だけを記録できる。手数料0円と明示された場合は、0円のcost eventを残す。未報告を0円と推定しない。

## draftとapply

1. 確認した事実からtyped draftを作る。
2. 人間がpayloadとcash・予約・holdingの差分を確認する。
3. `position apply-draft <DRAFT> --db stores/application/baibai.sqlite --confirmed`で適用する。入力状態が変わりstaleになった場合は、draftを作り直して再確認する。

新しいOperationは開始しない。取引事実の保存を無関係なmarket/market-context不足で止めず、公開actionから売買数量を推定しない。反映が必要なら[Ops Maintenance](../ops-maintenance/SKILL.md)へ進む。

## Portfolio outcome

outcomeはledger eventではなく、専用commandで評価・発行する。[Historical outcome](../../../docs/reference/portfolio-ledger.md#historical-outcome)に従い、同期間のbenchmarkと税・費用basisを確認する。unresolvedなら正常な結果として発行せず、不足を解消して再評価する。評価可能にするためにledgerの事実を消さない。
