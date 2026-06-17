# components/trades.md

Baibai-Loop の **execution record / trades** 成分の運用仕様。investment memo で採用され、実際に order / entry した判断の注文、約定、建玉、決済を記録する。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md) を参照。

## 1. 役割

- `records/05-research/` の approved memo から発生した **order intent / order / execution / position** を記録する
- 実際に order を作った場合のみ `records/06-trades/` を作る
- 採用したが発注しなかった判断、保留、見送り、未処理候補は decision register / reviews 側で扱う

## 2. Path と命名

```text
records/06-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
```

日付は最初の execution event を記録した日。未約定注文でも、最初の order submit 日を使い、後続の約定・取消・決済で改名しない。

## 3. Front Matter

```yaml
---
trade_id: trade-YYYYMMDD-<ticker>
ticker: "7203"
name: "トヨタ自動車"
research_ref: records/05-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook_id>.md
position_state: none | open | closed
trade_execution_state: none | submitted | broker_rejected | cancelled | expired | not_filled | partially_filled | filled
order_intent:
  order_intent_id: intent-YYYYMMDD-<ticker>-buy
  decision_event_id: decision-YYYYMMDD-<ticker>-trade
  side: buy | sell
  quantity: 100
  order_price_guard_yen: 1050
  expires_at: "YYYY-MM-DDTHH:MM:SS+09:00"
orders:
  - order_id: order-YYYYMMDD-<ticker>-buy-1
    origin_order_intent_id: intent-YYYYMMDD-<ticker>-buy
    external_broker_order_id: null
    side: buy | sell
    state: submitted | broker_rejected | cancelled | expired | not_filled | partially_filled | filled
    submitted_quantity: 100
    filled_quantity: 0
    order_price_guard_yen: 1050
executions: []
capital_basis:
  real_capital_yen: 5000000
  tactical_real_budget_yen: 1000000
  paper_proxy_capital_yen: 100000000
position_sizing_overlay:
  estimated_real_order_notional_yen: 210000
  guarded_max_notional_yen: 210000
planned_exit:
  target_price: 1200
  stop_loss: 950
  time_stop_days: 40
kill_switch_check:
  earnings_straddle: false
  boj_eve: false
  fomc_eve: false
accounting:
  cost_basis_method: weighted_average | fifo | lifo | specific_lot
execution_costs:
  commission_yen: null
  tax_yen: null
  slippage_yen: null
---
```

## 4. State Model

`trade_execution_state` は execution intent の最終状態、`orders[].state` は各 order の状態、`position_state` は executions から検証される建玉状態を表す。

| state | 意味 |
| --- | --- |
| `none` | order が作られていない。trade record では通常使わず decision register の approved-not-submitted で扱う |
| `submitted` | order が broker / API に受け付けられ、未約定または約定待ち |
| `broker_rejected` | broker / SOR により order が拒否された。research rejection とは別概念 |
| `cancelled` | user initiated cancel |
| `expired` | time-in-force などにより自動失効 |
| `not_filled` | guard price などにより約定しなかった |
| `partially_filled` | 一部約定し、残数量が未完了 |
| `filled` | order が全数量約定 |

`orders[].origin_order_intent_id` は decision register の `order_intent.order_intent_id` と一致させる。broker 側 ID は `external_broker_order_id` として任意で分離する。

## 5. Sizing / Guard

Order quantity は code-managed policy config の board lot と guard price から deterministic に算出する。

```text
target_quantity = floor(real_order_intent_yen / order_price_guard_yen / board_lot) * board_lot
guarded_notional_yen = target_quantity * order_price_guard_yen
```

`guarded_max_notional_yen` は cap 検査の正本であり、`estimated_real_order_notional_yen` は research / decision register から引き継ぐ実注文 intent の推定値である。rounded quantity が cap を超える場合は board lot 単位で減額する。`target_quantity == 0` の場合は trade record を submit せず、decision register 側で `trade_execution_state: none` と `not_submitted_reason: below_board_lot_minimum` を記録する。

`capital_basis.real_capital_yen` は実資金全体、`capital_basis.tactical_real_budget_yen` は当面投入する real budget、`capital_basis.paper_proxy_capital_yen` は paper / proxy sizing の仮想資本である。これらを混同しない。

## 6. Validator

trade record は `baibai-loop-validate` で `src/baibai_loop/validate/trade.py` が enforce する。

```bash
uv run baibai-loop-validate --target trade
```

主な enforced rule:

- 必須 front matter field の存在
- ticker 形式と filename との一致
- `orders[].origin_order_intent_id` と `order_intent.order_intent_id` の join
- `trade_execution_state != none` の場合、`research_ref` が approved research を参照していること
- `trade_execution_state != none` の場合、`order_intent.quantity > 0` であること
- `trade_execution_state != none` の場合、`position_sizing_overlay.estimated_real_order_notional_yen` と `guarded_max_notional_yen` が存在すること
- order state 値域と filled quantity consistency
- `position_state: none` と executions の矛盾検出
- guarded notional と `quantity * order_price_guard_yen` の一致

## 7. 本文の構成

### 7.1 Order / Entry

- **Entry reason**: investment memo の thesis / payoff / macro context fit / policy pass を要約
- **Entry preflight summary**: research の `Entry preflight` 結論を確認し、`proceed` / `starter` / `defer` / `exception` の扱いと未解消 blocker がないことを要約する。preflight の計算本体は research 側に置き、trade では再計算しない
- **Order / Entry triggers**: 実際に order を作った条件
- **Order state**: `orders[].state` と `executions[]` で現在状態を記録する。submit / modify / cancel / reject の時系列監査ログは残さない
- **Position**: real notional、guarded notional、target / stop / time stop

### 7.2 保有中

- 重大な macro / event / catalyst / positioning-liquidity 変化
- invalidation condition の接近
- kill switch の発火有無

### 7.3 Exit

- Exit reason
- Execution log
- Gross / net return と execution costs

## 8. Reviews への接続

- Review / retro では outcome を evidence hit、macro context fit、sizing、execution、playbook へ帰属する

## 9. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| order / execution log の整備 | ○ | |
| kill switch check 確認補助 | ○ | **最終判定は人間** |
| P&L / cost 計算 | ○ | |
| **実際の発注・取消・決済操作** | | ○ |
| **exit 判断** | | ○ |

自動発注はスコープ外。

## 10. 参考

- [`../philosophy.md`](../philosophy.md): 思想
- [`../architecture/system-overview.md`](../architecture/system-overview.md): 全体構造
- [`research.md`](./research.md): source となる research の仕様
- [`../templates/trade.md`](../templates/trade.md): template
