---
title: "Portfolio ledger reference"
summary: "repo内portfolioのcash、予約、約定、保有、income、cost、taxを再計算するevent契約。"
doc_type: reference
status: active
last_reviewed: 2026-07-12
---

# Portfolio ledger

## Scope

ledgerは`portfolio_scope: repository_only`だけを許し、このrepositoryで管理する日本株以外を合算しない。運用時のcanonical pathは`records/04-position/portfolio-ledger.yaml`である。ファイルが存在しない状態は未初期化であり、外部保有から推測して補完しない。

## Activation boundary

この文書とschemaはledgerの永続化契約を定義する。canonical ledgerはrepository内portfolioのhuman-confirmed cash、保有、未約定引当の正本であり、validatorはこの契約だけを検証する。broker残高を自動取得・推定・完全照合する契約ではない。

公開schemaは`records/_schemas/portfolio-ledger.json`、実装は`src/baibai_loop/position/ledger.py`を正本とする。schemaはunknown fieldと、event総額におけるfloat円額を拒否する。単価は小数4桁まで許すが、数量との積が1円単位に一致しないeventを暗黙に丸めず拒否する。

## Events

| event | cash / position effect |
| --- | --- |
| `opening_balance` | 初期available cashを設定する。ledger内で1件だけ |
| `contribution` | available cashを増やす。月次標準額は[`../portfolio-management.md`](../portfolio-management.md)を正本とする |
| `withdrawal` | available cashだけを減らす。予約・保有は暗黙に解約しない |
| `reservation` | `quantity * price_guard_yen`をavailableからreservedへ移す |
| `release` | 未約定残数のguarded notionalをreservedからavailableへ戻す |
| `execution` buy | filled分をreservedから取得原価へ移し、価格改善分をavailableへ戻す |
| `execution` sell | repo内FIFO lotを減らし、売却代金をavailableへ加える |
| `income` | 確認済み配当等をavailableとconfirmed incomeへ加える |
| `cost` | 確認済み手数料等をavailableから引く |
| `tax_confirmed` | 実際に確認した税額をavailableから引く |

reservation ID、order identity、execution IDは再利用しない。これらはledger replay用のrepository identityであり、brokerが同名IDを報告したことを意味しない。buy executionはactive reservation、同じticker、残数量以下、guard価格以下、expiry前を必須とする。expiry到達後は`release(reason=expired)`を明記し、暗黙解放しない。

`record-result`が作るreservation、execution、releaseはproposal/approval Issue URLを`decision_reference`に持つ。既存migration eventはこのfieldを持たない場合がある。active reservationにreferenceがあるfill/cancelは同じreferenceだけを受け付け、別の判断へ付け替えない。

人間報告から作る`reservation / execution / release`はproposal/approval URLを`decision_reference`に持つ。既存migration eventではnullを許すが、新しいhuman resultは参照なしで記録しない。

`event_id`が`migration-`で始まるeventは、移行時点の保有・予約をcanonical stateへ初期化する記録であり、人間が報告したbroker注文・約定・取消ではない。`baibai-loop-position ledger`はこれらを`event_annotations`の`ledger.migration-initialization`として件数表示する。期間内の新規broker resultを数えるときはmigration eventを含めず、`record-result`へ入力された人間報告と`decision_reference`を基準にする。この注記は表示上の区別であり、reconciliation計算やevent modelを分岐させない。

reservationとexecutionの数量はpolicyの`board_lot`倍数に限定する。小数単価は1 board lotとの積が整数円になる場合だけ受理するため、合法な部分約定ごとのreserved cashも暗黙の丸めなしに再計算できる。

## Snapshot equations

```text
available_cash = cash inflows - active reservations - executions - confirmed costs/tax
reserved_cash = sum(active remaining_quantity * price_guard_yen)
deployed_cost = sum(open FIFO lot quantity * execution price)
book_capital = available_cash + reserved_cash + deployed_cost
total_capital = available_cash + reserved_cash + holdings_market_value
```

pending orderはreservation eventを1回だけ持ち、partial fill後は未約定残数だけをreservedに残す。これにより同じ注文の二重引当を防ぐ。

## Errors and warnings

Hard error:

- available cashを超えるreservation / cost / confirmed tax
- 重複ID、未知reservationへのrelease/execution、保有超過sell
- guard超過、expiry以後のbuy execution、明示releaseのないexpired reservation
- event順序、future event/price/override、ticker metadataの不整合

Warning:

- current holding market value + active reservationがticker / sector / common-factor warning lineを超える
- available cash比率がdry-powder warning lineを下回る

warningは判断を禁止しない。overrideは`reason`、`decision_reference`、approval/expiryを必須とし、policyの最大31日を超えられない。

## Tax estimate

`income`と売却代金はgrossで記録し、手数料は`cost`、確認済み税額は`tax_confirmed`へ別eventとして記録する。snapshotは`confirmed_cost_yen`と`confirmed_tax_yen`を分離し、互換的な合計`confirmed_cost_tax_yen`も返す。

`tax_confirmed`はcashへ反映する実績である。将来売却税を表示する場合は、`estimated_exit_tax_rate_bps`と`estimated_exit_tax_basis: ledger_fifo_gross_unrealized_gain`を同時に指定する。このestimateはledger内FIFO取得原価に対する銘柄別gross含み益の正値だけを対象とし、手数料、損益通算、口座種別は扱わない。cashやconfirmed taxへ混ぜず、未指定時の`estimated_exit_tax_yen`は`null`とする。

保有時価には各tickerの`observed_at`、`source_kind`、`price_basis`、`source_ref`を必須とする。`source_kind`はmarket API・取引所・契約dataset・test fixtureを、`price_basis`は現在値・終値・未調整終値を区別する。test fixtureはテスト成果物だけで使う。`observed_at`がpolicyの`market_price_max_age_days`を超える場合はsnapshotを生成しない。

## Commands

```bash
uv run baibai-loop-position ledger
uv run baibai-loop-position record-result --help
uv run baibai-loop-validation --target ledger
```

`record-result`は人間の`open / filled / cancelled`報告だけを入力とし、canonical ledgerを直接変更しない。source ledger hashとpatched local draftを返す。報告がない状態、missing field、未知reservation、future timestamp、reconciliation errorを推定で補わない。draftのevent/snapshot/diffを確認し、source hash不変とvalidationを確認してからcanonicalへ反映する。

representative contract fixtureは`tests/fixtures/portfolio-ledger/representative.yaml`に置く。

<a id="historical-outcome"></a>

## Historical outcome

portfolio outcome はledger eventを各JPX営業日closeまで再生し、日次NAVを
`available_cash + reserved_cash + open holdings market value` として算出する。
`contribution`だけを正、`withdrawal`だけを負のexternal flowとし、buy/sell、reservation、配当、費用、確定税はNAV内部のeventである。開始日を除く各営業日のreturnは次で連鎖する。

```text
r_d = V_d / (V_(d-1) + CF_d) - 1
```

非営業日のeventは次のJPX営業日のBODへ繰り越す。価格欠損、未解決のcorporate action、ゼロ以下NAVは補完せずoutcomeを`unresolved`にする。`estimated_exit_tax_yen`は将来仮定の表示であり、実績returnへ入れない。
