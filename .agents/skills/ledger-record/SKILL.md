---
name: ledger-record
description: 人間が確認した注文・約定・資金の事実をdraftで確認し、台帳へ記録する。
---

# Ledger Record

人間が報告した事実を記録する。現在の投資条件で実取引を再審査せず、提案から採用・発注・約定・cashを推測しない。[Portfolio Ledger](../../../docs/reference/portfolio-ledger.md)が意味を所有する。

## 事実からdraftを作る

| 報告 | 入口 |
| --- | --- |
| 初回残高・既存保有 | `position opening-balance-draft` |
| 買付注文・約定、売付の未約定注文 | `position broker-fact-draft`（売付は`--side sell`） |
| 売却・部分売却の約定 | `position sell-execution-draft` |
| 入出金・income・費用・確定税 | `position event-draft` |
| 人間のwarning受容・税の見積り | `position override-draft`・`position meta-draft` |

提案に対応する注文は公開したInvestment Proposal IDと、人間が確認した実注文条件を使う。

```bash
uv run baibai-engine position broker-fact-draft --db stores/application/baibai.sqlite \
  --decision-reference <INVESTMENT_PROPOSAL_ID> --status open \
  --ticker <TICKER> --quantity <QTY> --price-guard-yen <GUARD> \
  --occurred-on <YYYY-MM-DD> --sector <SECTOR> --out <DRAFT>
```

提案なしの実買付は`--decision-reference`の代わりに`--direct-fact`を使う。初回に返されたmanual-fact参照を後続・再試行で保持し、同じ報告へ新しい参照を発行しない。直接取引のために評価や提案を作らない。

約定の日付・銘柄・数量・単価と報告されたbroker execution IDを使い、保存注文があれば同じreservationと参照へ束縛する。[事実と再送](../../../docs/reference/portfolio-ledger.md#事実と再送)に従い、同日別約定を区別できない場合だけ確認する。正確な報告時刻は`--occurred-at`へ渡せる。

```bash
uv run baibai-engine position sell-execution-draft --db stores/application/baibai.sqlite \
  --ticker <TICKER> --quantity <QTY> --price-yen <PRICE> \
  --occurred-on <YYYY-MM-DD> --execution-id <BROKER_EXECUTION_ID> --out <DRAFT>
```

[cash・費用の意味](../../../docs/reference/portfolio-ledger.md#human-result-semantics)に従い、業種等の既知入力は既存sourceから確認する。

## 確認と反映

payloadとcash・予約・holdingの差分を人間に確認し、`position apply-draft <DRAFT> --db stores/application/baibai.sqlite --confirmed`で適用する。staleなら新しい入力からdraftを作り直して再確認する。取引記録のためにOperationを開始せず、無関係なmarket不足で止めない。

本番への反映は[application反映](../../../batch/OPERATIONS.md#application-db-を反映する)へ進む。作業コピーへの適用とクラウド正本の更新を区別する。

Outcomeが依頼された場合は[historical outcome](../../../docs/reference/portfolio-ledger.md#historical-outcome)の専用commandと比較basisに従う。
