---
title: "Execution lifecycle reference"
summary: "人間承認、手動注文、broker確認済み約定を分離するmulti-intent execution contractの参照仕様。"
doc_type: reference
status: active
last_reviewed: 2026-07-11
---

# Execution lifecycle

`records/_schemas/execution-lifecycle.json` は、単一tickerの人間承認、brokerへの手動注文、brokerで確認した約定を分離する公開contractである。実装は `src/baibai_loop/position/execution.py`、schemaだけでは表せない整合性検証は `src/baibai_loop/validation/execution_lifecycle.py` を正本とする。

このcontractは現行の `position.json` とは独立に検証する。active position recordをこのcontractへ切り替える作業は、active stateを再構成する変更と同時に行う。旧/new fieldを同一recordへ混在させない。

## Responsibility

execution lifecycleは執行の証拠だけを持つ。

- `decision_intents[]`: exact decision packet hashとuser decision referenceに束縛した、人間承認済みの数量・価格上限/下限・期限
- `orders[]`: intentの範囲内でbrokerへ手動提出した個別指値。broker order ID、terminal status、terminal timestampは`null`を含めて常に明示する
- `executions[]`: brokerで確認したimmutableな約定

cash、reservation、FIFO holdings、income、cost、taxの唯一の正本は [`portfolio-ledger.md`](./portfolio-ledger.md) のledgerである。lifecycleは残高や集約約定状態を手入力で複製しない。

## Price and retry semantics

intentの`price_guard_yen`は、buyなら最大許容価格、sellなら最低許容価格である。orderの`limit_price_yen`はintent guardの内側でだけ設定できる。これにより、同じ人間承認の範囲で浅い/深い複数指値を出せる。

同じintentで許されるのは、同じ数量上限・期限・価格guardの範囲内のbroker retryまたは複数指値である。intentの数量、side、guard、expiryを変える場合は新しいdecision packetとuser confirmationを作る。

## Derived state

`execution_state`、`filled_quantity`、`entry_legs`、手入力`current_quantity`はこのcontractのsource of truthではない。pure evaluatorが次を導出する。

- orderごとのsubmitted / partially_filled / filled / broker_rejected / cancelled / expired
- intentごとのfilled / remaining quantity
- `sum(buy) - sum(sell)`によるcurrent quantity
- never-held / open / closed position state
- buy executionからのentry dateとweighted buy price

sell executionは任意時点のrepository holdingを超えられない。1つの`position_id`は1つの連続した保有roundだけを表し、全売却後の再購入は新しいlifecycleにする。期限到達後のexecution、guard違反、terminal order後のexecution、同時live orderによるintent quantity超過、同一broker timestampのbuy/sell混在はhard errorである。同一timestampのconfirmation、submit、execution、terminalはこの順に再生する。

## Ledger reconciliation

canonical ledgerが存在する場合、同じ`as_of`かつlifecycle最初のhuman confirmation以後の同一ticker reservation order IDとexecution IDの集合はlifecycleと完全に一致しなければならない。全売却後の再購入は新しいlifecycleにして、開始前のclosed roundをこの照合範囲から分ける。closed lifecycleは記録した`as_of`と同じledger snapshotだけで照合し、後続roundを含む将来snapshotとの照合はしない。buy orderごとにreservationを1件だけ対応させ、ticker、quantity、limit price、submit時刻、expiry、execution ID、execution時刻、side、priceを照合する。

buy orderの未約定残がbroker rejection、cancel、expiryで終わる場合、ledgerには同じreservationへの明示releaseが必要である。sell executionはreservationを持たない。lifecycleがledger eventを自動生成することはない。

## Safety boundary

`decision_reference` とpacket hashは、人間判断を対象proposalへ明示的に紐付けるためのrecordであり、本人性を証明するものではない。安全境界はbrokerのorder/cancel endpointを実装せず、人間だけがbrokerを操作することに置く。

## Verification

```bash
uv run pytest tests/test_position_execution.py
uv run baibai-loop-validation
```

代表fixtureは `tests/fixtures/execution-lifecycle/representative.yaml`、ledger照合fixtureは `tests/fixtures/execution-lifecycle/ledger.yaml` に置く。
