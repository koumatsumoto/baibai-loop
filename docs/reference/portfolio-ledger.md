---
title: "Portfolio ledger reference"
summary: "application DBのeventからcash、予約、約定、保有、income、cost、taxを再計算する契約。"
doc_type: reference
status: active
---

# Portfolio ledger

<a id="scope-and-canonical-home"></a>

## 対象範囲と正本

ledgerは`portfolio_scope: repository_only`だけを扱う。application DBの`ledger_event / ledger_meta`が確認済み資本・取引事実を持ち、価格を台帳へ重複保存しない。市場価格はL1市場ストアから評価時に読み、取引事実と独立した入力として渡す。日次終値の時点は原典の取引日`price_as_of`で表し、15:30などの観測時刻を捏造しない。取引日の利用可否はJSTの立会終了境界で判定し、価格の時点とは分ける。価格観測自体に「比較可能」フラグを持たせず、保有数量との単位確認と、評価元価格との比較基準はそれぞれの期間で判定する。`baibai-engine position ledger --db stores/application/baibai.sqlite`はeventをreplayし、market storeのquoteと権利単位を組み合わせてsnapshotを返す。broker残高を自動取得・推定・完全照合する契約ではない。

台帳文書はschema version 3。切替では旧価格行を新台帳へ移さず、必要な取引・設定を現行文書へ移すか、対象を確認して新規台帳から開始する。旧draftは再作成する。価格の再取得・評価は市場ストアの責務であり、旧台帳価格による代替は行わない。

DB constraint、`baibai_engine.position`のmodel、application serviceのwrite-time validationが機械契約を担う。台帳の再構築と保存値の検査は`position/ledger_read.py`が所有し、read APIとwriterが同じ実装を使う。connectionとtransactionはcallerが所有する。未作成・未書込・初期化済み未importだけを未登録として読み、table/column欠損・metadataなし取引・不正payloadを空保有へ変換しない。円総額は整数、単価は許可精度内、数量との積は1円単位に一致しなければ拒否する。

<a id="events"></a>

## Event

| event | cash / position effect |
| --- | --- |
| `opening_balance` | 初期cashを設定する。migration専用で1件だけ |
| `contribution` | available cashを増やす |
| `withdrawal` | available cashを減らす |
| `reservation` buy | guarded notionalをavailableからreservedへ移す |
| `reservation` sell | 保有株数の範囲で未約定売付を記録する。cash・lotは動かさない |
| `release` | 未約定分の注文を明示的に解放する。buyだけ現金をavailableへ戻す |
| `execution` buy | 注文対応分はreservedを解放し、買付代金を取得原価へ移す。注文未登録の実約定はavailableから直接支払う |
| `execution` sell | FIFO lotを減らし、売却代金をavailableへ加える |
| `income` | 確認済み配当等をavailableとconfirmed incomeへ加える |
| `cost` | 確認済み費用をavailableから引く |
| `tax_confirmed` | 確認済み税額をavailableから引く |

event ID、reservation ID、order identity、execution IDは再利用しない。新しいbuyのbroker factは具体buy条件を公開したInvestment Proposal ID、または明示した直接取引のmanual-fact参照へ束縛する。約定の記録に事前注文の登録・発注時刻・期限を要求しない。保存注文へ対応付ける場合は同じticker、remaining以下、guard以下を確認する。releaseは明示eventであり自動生成しない。

event rowはappend-onlyで、late reportも新規rowとして保存する。replay順は`(occurred_at, same_instant_order)`である。同時刻の既存eventの順序とIDを変更しない。
取引報告は日付を基本とし、`occurred_on`に保存する。注文・取消・失効にも正確な時刻を要求しない。`occurred_at`はreplay順・日付検索のための内部キーであり、実際の約定時刻を表さない。同日内は既存の記録より後へ追加するため、その日の最後のreplayキーを使う。同日の先行記録がなければ日本時間00:00を使う。これは実際の発注・約定時刻の推定ではない。
IDなしで記録した日付だけの買付報告を同じ内容で再送するとno-changeになる。同じ日・注文の別約定は、brokerが示した実約定を区別できる`execution_id`を指定する。brokerの約定IDが注文内だけで一意なら、実注文IDと実約定IDの組を渡す。同じID・同じ内容は予約消滅後もno-change、同じID・内容違いは拒否する。注文ID単独や推定時刻では区別しない。売付もIDなしの同日別約定を内容の違いだけで新規扱いしない。

<a id="snapshot-equations"></a>

## Snapshotの式

snapshotの`as_of`はreplay・評価の対象時点、`ledger_as_of`は保存台帳の記録基準である。市場quoteの観測時点はholdingごとに保持する。現在の再評価だけで台帳の記録基準や鮮度を更新しない。

```text
cash_before_reservations =
    opening_balance
    + contributions - withdrawals
    + sell_proceeds - buy_payments
    + confirmed_income - confirmed_costs - confirmed_tax

reserved_cash = sum(active buy remaining_quantity * price_guard_yen)
available_cash = cash_before_reservations - reserved_cash
deployed_cost = sum(open FIFO lot quantity * execution price)
realized_gross_pnl = sum(sell proceeds - consumed FIFO cost)
book_capital = available_cash + reserved_cash + deployed_cost
total_capital = available_cash + reserved_cash + holdings_market_value
```

各項は対象時点までの確認済み額で、売買代金はgross、費用・税は別eventである。`realized_gross_pnl`は保存台帳開始以来、対象時点までの全売却の損益を符号付きで通算した税・手数料控除前の額で、全株売却や買い直しでもリセットしない。株価から独立しており、売却代金は既にcashに含まれるため、総資産や未予約現金へ再加算しない。予約と解放を入出金へ二重計上しない。`available_cash`は売却代金を約定時に計上した台帳上の未予約現金であり、受渡済み資金や証券会社の発注可能額との照合結果ではない。時価未評価の場合、total capitalをcash-onlyの値で代用しない。

partial fill後は未約定残数だけをreservedに残す。hard errorはcash超過、重複ID、未知reservation、overfill / oversell、guard超過、future row、metadata不整合。concentrationとdry powderはwarningであり、判断を禁止しない。

<a id="market-price-and-tax"></a>

## Market priceと税

現在の時価は`position/valuation.py`がJ-Quants raw/unadjusted closeと現在の保有episodeの権利basisを読み、台帳への価格転記なしで評価する。quoteの欠損・古さや権利basis不明は対象holdingの時価を未評価にし、cash・予約・数量・原価を保持する。全保有を評価できなければNAVと集中比率も未評価とする。adjusted closeや保存済みの台帳価格で補完しない。株数の基準と価格比較の基準は分ける。ExRTがライツイシューを示す価格調整は既存株の数量を倍率換算しないが、調整前後の価格を無調整のまま比較してよいことにはならない。権利の売却・行使・追加取得は実際の取引記録から扱い、この数量判定から推測しない。

`income`とsell proceedsはgross、feeは`cost`、確認済み税は`tax_confirmed`に分離する。手数料0円の明示報告はamount 0のcost eventとして保存できる。費用・税のeventがないことは実費・実税がゼロと確定した意味ではない。estimated exit taxは`ledger_meta`のrateと`ledger_fifo_gross_unrealized_gain` basisから表示だけを計算し、cashやconfirmed taxに混ぜない。

<a id="draft--apply-contract"></a>

## Draftとapplyの契約

`broker-fact-draft`、`sell-execution-draft`、`event-draft`、`override-draft`、`meta-draft`はcanonical DBを変更しない。draftはsource append headと置換対象rowを持つ。人間が内容を確認した後だけ次を実行する。

```bash
uv run baibai-engine position apply-draft /tmp/ledger-draft.yaml --db stores/application/baibai.sqlite --confirmed
```

applyは1 transactionでsource head、event replay / payload、price/meta expected row、reconciliationを再検証する。`--confirmed`なし、stale、矛盾payloadはno-writeである。

すべての取引事実draftは価格不要のevent replayでcash・reservation・lotを検証する。別注文の期限未解決を理由に、確認済み取引の保存を止めない。実売却、入出金、income、cost、税の記録に市場価格の更新を要求しない。readerは未報告の予約を保持し、時刻だけからreleaseを推定しない。

<a id="human-result-semantics"></a>

## 人間が報告するbroker fact

明示報告がない場合の扱いと確認する範囲は[Ledger Recordの売買報告の扱い](../../.agents/skills/ledger-record/SKILL.md#売買報告の扱い)に従う。

`position/drafts.py`がdraft組立と予約束縛を所有する。active予約・既知terminalに束縛されないbuy報告は`position_decision_reference`の保存bindingからtickerを解決する。現在の評価・提案適格性を再審査せず、buy以外の参照や銘柄不一致を拒否する。提案なしの買付は`--direct-fact`で明示し、draftに固定したmanual-fact参照をledgerと同じtransactionで保存する。旧判断のbindingも取引識別として保全し、旧payloadを新規評価として読む互換経路は持たない。active予約と既知terminal（cancel / expire）の再試行は既存の束縛を使う。実売却の判断参照は任意であり、提案やPosition Review artifactを作成することを事実記録の前提にしない。

`broker-fact-draft`は人間の`open / filled / cancelled / expired`報告だけを入力にする。active reservationをIDなしで推定しない。partial fillはremainingがある間だけ後続broker factを受理する。full fill / cancel / expire後の完全一致broker fact reportはno-change、矛盾broker fact reportはhard errorとする。`expired`は人間が未約定での失効を確認した場合にreleaseを作る。期限が保存されている場合だけ、期限前の報告との矛盾を検証する。同時刻に複数reservationがterminalになる場合は`--reservation-id`の反復指定を1 transactionで検証・適用する。対象の一部が不正なら全件を拒否する。active reservationの後続broker factは同じ`decision_reference`、reservation ID、order IDへ束縛する。

売付の`open`は`broker-fact-draft --side sell`で記録し、保有株数を超える重複注文を拒否する。売付の見込代金を現金へ加えず、対応する約定報告までlotを減らさない。別の売付注文があっても、他注文へ拘束されていない保有株数の範囲なら独立した実売却を記録できる。その約定を既存注文へ推定対応付けたり、その注文の残数を減らしたりしない。拘束済み株数も売った報告は、対応する注文IDまたは取消事実を明示する。
注文は`--occurred-on`で日付だけを記録できる。期限は任意で、未指定でも注文・約定の記録を妨げない。「今週中」などは`expiry_label`に原文を保存できるが、正確な期限への変換や追加確認を要求しない。既知の`expires_at`は排他的な期間終端として扱う。注文画面の市場・SOR等は`route_label`に原文を残し、約定した実市場とみなさない。broker固有の正確な失効時刻を意味しない。

<a id="historical-outcome"></a>

## Historical outcome

公式TOPIX gross-total-returnの観測・期間・出所検証は`market_context/observations/benchmark`が所有し、保存済みJPXファイル読取は`market/jpx_total_return`へ委譲する。Nikkei ETF price-onlyの市場方向評価をtotal-return benchmarkへ転用しない。

portfolio outcomeは各JPX営業日closeまでeventをreplayし、日次NAVを`available_cash + reserved_cash + open holdings market value`として算出する。`contribution`を正、`withdrawal`を負のexternal flowとし、buy/sell、reservation、income、cost、taxはNAV内部eventである。returnに反映する費用・税は記録済みeventに限り、`resolved`は未報告分の不存在や税・費用の完全反映を保証しない。

```text
r_d = V_d / (V_(d-1) + CF_d) - 1
```

非営業日のeventは次のJPX営業日BODへ繰り越す。価格欠損、未解決corporate action、ゼロ以下NAVは補完せず`unresolved`にする。outcome publicationはapplication DBのimmutable recordである。
