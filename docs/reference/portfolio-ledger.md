---
title: "Portfolio ledger reference"
summary: "application DBのeventからcash、予約、約定、保有、income、cost、taxを再計算する契約。"
doc_type: reference
status: active
---

# Portfolio ledger

<a id="scope-and-canonical-home"></a>

## 対象範囲と正本

ledgerは`portfolio_scope: repository_only`だけを扱う。application DBの`ledger_event / ledger_meta`が確認済み資本・取引事実を持ち、保存済みの`ledger_market_price`は既存記録として保持する。`baibai-engine position ledger --db stores/application/baibai.sqlite`はeventをreplayし、market storeのquoteと権利単位を組み合わせてsnapshotを返す。broker残高を自動取得・推定・完全照合する契約ではない。

DB constraint、`baibai_engine.position`のmodel、application serviceのwrite-time validationが機械契約を担う。台帳の再構築と保存値の検査は`position/ledger_read.py`が所有し、read APIとwriterが同じ実装を使う。connectionとtransactionはcallerが所有する。未作成・未書込・初期化済み未importだけを未登録として読み、table/column欠損・metadataなし取引・不正payloadを空保有へ変換しない。円総額は整数、単価は許可精度内、数量との積は1円単位に一致しなければ拒否する。

<a id="events"></a>

## Event

| event | cash / position effect |
| --- | --- |
| `opening_balance` | 初期cashを設定する。migration専用で1件だけ |
| `contribution` | available cashを増やす |
| `withdrawal` | available cashを減らす |
| `reservation` | guarded notionalをavailableからreservedへ移す |
| `release` | remaining guarded notionalをavailableへ戻す |
| `execution` buy | filled分をreservedから取得原価へ移し、価格改善分をavailableへ戻す |
| `execution` sell | FIFO lotを減らし、売却代金をavailableへ加える |
| `income` | 確認済み配当等をavailableとconfirmed incomeへ加える |
| `cost` | 確認済み費用をavailableから引く |
| `tax_confirmed` | 確認済み税額をavailableから引く |

event ID、reservation ID、order identity、execution IDは再利用しない。新しいbuyのbroker factはcanonical buy assessment IDへ束縛する。buy executionはactive reservation、同じticker、remaining以下、guard以下、expiry以前を必須とする。releaseは明示eventであり自動生成しない。

event rowはappend-onlyで、late reportも新規rowとして保存する。replay順は`(occurred_at, same_instant_order)`である。同時刻の既存eventの順序とIDを変更しない。

<a id="snapshot-equations"></a>

## Snapshotの式

```text
available_cash = cash inflows - active reservations - executions - confirmed costs/tax
reserved_cash = sum(active remaining_quantity * price_guard_yen)
deployed_cost = sum(open FIFO lot quantity * execution price)
book_capital = available_cash + reserved_cash + deployed_cost
total_capital = available_cash + reserved_cash + holdings_market_value
```

partial fill後は未約定残数だけをreservedに残す。hard errorはcash超過、重複ID、未知reservation、overfill / oversell、guard超過、expiry後buy、future row、metadata不整合。concentrationとdry powderはwarningであり、判断を禁止しない。

<a id="market-price-and-tax"></a>

## Market priceと税

現在の時価は`position/valuation.py`がJ-Quants raw/unadjusted closeと現在の保有episodeの権利basisを読み、台帳への価格転記なしで評価する。quoteの欠損・古さや権利basis不明は対象holdingの時価を未評価にし、cash・予約・数量・原価を保持する。全保有を評価できなければNAVと集中比率も未評価とする。adjusted closeや保存済みの台帳価格で補完しない。

`income`とsell proceedsはgross、feeは`cost`、確認済み税は`tax_confirmed`に分離する。estimated exit taxは`ledger_meta`のrateと`ledger_fifo_gross_unrealized_gain` basisから表示だけを計算し、cashやconfirmed taxに混ぜない。

<a id="draft--apply-contract"></a>

## Draftとapplyの契約

`broker-fact-draft`、`sell-execution-draft`、`event-draft`、`override-draft`、`meta-draft`はcanonical DBを変更しない。draftはsource append headと置換対象rowを持つ。人間が内容を確認した後だけ次を実行する。

```bash
uv run baibai-engine position apply-draft /tmp/ledger-draft.yaml --db stores/application/baibai.sqlite --confirmed
```

applyは1 transactionでsource head、assessment / reservation、event payload、price/meta expected row、reconciliationを再検証する。`--confirmed`なし、stale、buyでないassessment、broker fact reportなし、矛盾payloadはno-writeである。

すべての取引事実draftは価格不要のevent replayでcash・reservation・lotを検証する。`broker-fact-draft`のapplyは未解放expiryも再検証する。実売却、入出金、income、cost、税の記録に市場価格の更新を要求しない。readerは未報告の予約を保持し、時刻だけからreleaseを推定しない。

<a id="human-result-semantics"></a>

## 人間が報告するbroker fact

`broker-fact-draft`は人間の`open / filled / cancelled / expired`報告だけを入力にする。active reservationをIDなしで推定しない。partial fillはremainingがある間だけ後続broker factを受理する。full fill / cancel / expire後の完全一致broker fact reportはno-change、矛盾broker fact reportはhard errorとする。`expired`は人間が未約定を確認し、`occurred_at >= expires_at`の場合だけreleaseを作る。同時刻に複数reservationがterminalになる場合は`--reservation-id`の反復指定を1 transactionで検証・適用する。対象の一部が不正なら全件を拒否する。active reservationの後続broker factは同じ`decision_reference`、reservation ID、order IDへ束縛する。

<a id="historical-outcome"></a>

## Historical outcome

portfolio outcomeは各JPX営業日closeまでeventをreplayし、日次NAVを`available_cash + reserved_cash + open holdings market value`として算出する。`contribution`を正、`withdrawal`を負のexternal flowとし、buy/sell、reservation、income、cost、taxはNAV内部eventである。

```text
r_d = V_d / (V_(d-1) + CF_d) - 1
```

非営業日のeventは次のJPX営業日BODへ繰り越す。価格欠損、未解決corporate action、ゼロ以下NAVは補完せず`unresolved`にする。outcome publicationはapplication DBのimmutable recordである。
