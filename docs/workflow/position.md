---
title: "Workflow — position (execution & holding)"
summary: "売買執行記録：approved thesis の注文・約定・長期保有・押し目買増し・割高で全売りを記録し、見積り（RR・期待利回り）と実現結果を calibrate する。"
doc_type: workflow
status: active
last_reviewed: 2026-07-01
---

# Workflow — 執行・保有（position）

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の執行・保有・calibration 工程。[`./research.md`](./research.md) で `approved` した判断の注文・約定・長期保有・押し目買増し・**割高で全売り**を記録し、entry 時の見積り（RR・期待利回り・FV）を **実現結果と突き合わせて calibrate** する（＝運用の改善）。自動発注はしない。契約の正本は `records/_schemas/position.json`。

## Before writing

1. 対応する research が `thesis_decision.outcome: approved` であることを確認する。`thesis_ref` が実在するか。
2. research の `entry_preflight` が `proceed` / `starter` で未解消 blocker がないか。
3. 集中度は [`../portfolio-management.md`](../portfolio-management.md) の cap（ticker 4–6% / sector 30–40%、entry 時 sizing 制約・分母は `real_capital_yen` 簿価）で確認する。
4. 注文種別（成行・指値・寄成）、休場日、次回立会日を確認する。休場日・立会時間外は `orders[].state: submitted` とし、約定価格を推定で埋めない（[`../anti-patterns.md`](../anti-patterns.md) AP-09）。

## Path と front matter

```text
records/06-position/YYYY/MM/YYYY-MM-DD-<ticker>.md
```

日付は最初の execution event の日。front matter の完全形は `records/_schemas/position.json`（contract-of-record）を正とし、下は形の確認用の最小例。

```yaml
position_id: trade-YYYYMMDD-XXXX
ticker: "XXXX"
name: "..."
thesis_ref: records/05-thesis/YYYY/MM/YYYY-MM-DD-XXXX-<playbook_id>.md
position_state: none | open | closed
execution_state: none | submitted | broker_rejected | cancelled | expired | not_filled | partially_filled | filled
order_intent: { order_intent_id: intent-YYYYMMDD-XXXX-entry, side: buy | sell, quantity: 100, order_price_guard_yen: 1000, uses_margin: false }
orders: [ { order_id: order-YYYYMMDD-XXXX-1, origin_order_intent_id: intent-YYYYMMDD-XXXX-entry, side: buy, state: filled, submitted_quantity: 100, filled_quantity: 100 } ]
executions: [ { execution_id: exec-..., order_id: order-..., side: buy, quantity: 100, price_yen: 1000, at: "YYYY-MM-DDTHH:MM:SS+09:00" } ]
capital_basis: { real_capital_yen: 10000000 }
position_sizing_overlay: { estimated_real_order_notional_yen: 100000, guarded_max_notional_yen: 100000 }
review_valuation: { as_of: "YYYY-MM-DD", fair_value_yen: 1400, current_price_yen: 1050, valuation_zone: cheap | fair | rich, action: hold | add | sell }
kill_switch_check: { earnings_straddle: false, boj_eve: false, fomc_eve: false }
estimate_calibration: { entry_expected_upside_pct: 40.0, entry_expected_yield_pct: 0.0, realized_return_pct: null, realized_yield_pct: null, thesis_held: null }
```

`capital_basis` は **単一プール** `real_capital_yen` のみを持つ。exit は valuation（割高化）と fundamental 毀損で判断し、価格・時間による自動 exit の field は置かない。

## State model

`execution_state` は execution intent の最終状態、`orders[].state` は各 order の状態、`position_state` は executions から検証される建玉状態。`orders[].origin_order_intent_id` は order_intent と一致させる。`orders[].filled_quantity <= submitted_quantity`。`position_state: none` は executions を持たない。

## 買い・長期保有・押し目買増し

- **買い**：research の割安ゾーン ∧ FV 下方乖離で entry。指値 guard price と board lot で数量を丸める。
- **長期保有**：価格の逆行では切らない。含み損でも塩漬け耐性が維持される限り保有する。
- **押し目買増し**：既保有銘柄が更に割安化し、cap 内で余力があれば買い増す（entry 時 cap を分母 `real_capital_yen` で確認）。

## 割高で全売り

売りトリガーは 2 つだけ：**(a) 割高化**（現値が FV へ収束・割高ゾーン到達）、**(b) 事業の fundamental 毀損**（減益トレンド・財務悪化・減配・thesis 中核崩壊）。いずれも **全売り**（部分トリムはしない）。exit の `executions[]` と gross / net return・execution costs を記録する。

## 保有見直しと見積り calibration

- **定例見直し**：**月次**（積立と同期）と **決算後** に、各保有の `review_valuation`（FV・現値・valuation zone・action）を更新する。割高ゾーン到達なら全売り、割安維持なら保有 / 買増し。決算後見直しが要る保有は GitHub Issue（`task:earnings-review` ラベル、`task: YYYY-MM-DD <ticker> を <event> 後に確認する`）で実行漏れを防ぐ。判断の正本は records に戻す。
- **見積り calibration**：exit / 決算後に `estimate_calibration` を更新し、entry の見積り（expected upside・期待利回り）と実現結果（realized return・yield・thesis 的中）を突き合わせる。系統的なズレ（macro 読み・FV 推定・耐性判定のどこが外れたか）を次の見積りへ反映する（＝改善ループ、[`../doctrine.md`](../doctrine.md) 柱 3）。保有の対 benchmark 相対リターンは `uv run baibai-loop-position benchmark`（`1321` proxy、[`../reference/data-sources.md`](../reference/data-sources.md)）で機械算出し、calibration の参考情報にする。

## Kill switch check

`kill_switch_check` は保有中の継続監視として記録する。fundamental 毀損を検知したら「全売り (b)」で exit。binary event（決算跨ぎ・日銀会合前日・FOMC 前日）直前の新規建玉は避けるか小さくする（長期積立では hard block ではない、[`../portfolio-management.md`](../portfolio-management.md)）。

## AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| order / execution log・P&L / cost 計算・calibration の集計 | ○ | |
| **実際の発注・取消・決済操作** | | ○ |
| **exit 判断・kill switch 最終判定** | | ○ |

## Validation

```bash
uv run baibai-loop-validation --target position
uv run baibai-loop-validation
```

必須 field、ticker と filename 一致、order intent と orders の join、order state 値域と filled quantity consistency、`position_state: none` と executions の矛盾検出、guarded notional と `quantity * order_price_guard_yen` の一致を検査する。

## 参考

- [`./research.md`](./research.md)：approved thesis と FV・見積り
- [`../portfolio-management.md`](../portfolio-management.md)：cap・kill switch・全売り規律
- [`../doctrine.md`](../doctrine.md)：柱 3（見積り calibration）
- [`../anti-patterns.md`](../anti-patterns.md)：AP-09（会社 IR 一次確認・注文/約定の状態分離）
