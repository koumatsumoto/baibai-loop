---
title: "Workflow — position (execution & holding)"
summary: "売買執行記録：approved thesis の注文・約定・長期保有・押し目買増し・割高で全売りを記録し、見積り（RR・期待利回り）と実現結果を突き合わせて較正する。"
doc_type: workflow
status: active
last_reviewed: 2026-07-11
---

# Workflow — 執行・保有（position）

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の執行・保有・較正の工程。[`./research.md`](./research.md) で `approved` になった判断について、注文・約定・長期保有・押し目での買増し・**割高での全株売却**を記録し、entry 時の見積り（リスクリワード・期待利回り・FV）を **実現結果と突き合わせて較正する**（= 運用の改善）。自動発注はしない。契約の正本は `records/_schemas/position.json`。

`records/04-position/portfolio-ledger.yaml`が存在する場合だけledger手順を実行する。未初期化の間はactive position recordと既存のposition/thesis concentration gateを使い、推測でledgerを作らない。切替条件は[`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)のActivation boundaryに従う。

## 書く前の確認

1. 対応する research が `thesis_decision.outcome: approved` であること。`thesis_ref` の実在。
2. research の `entry_preflight` が `proceed` / `starter` で、未解消の妨げが残っていないこと。
3. canonical ledger稼働時は`baibai-loop-position ledger`でavailable cash、既存reservation、注文後のticker / sector / common-factor warningを確認する。warning超過を受け入れる場合は理由と期限付きoverrideをledgerへ記録する。未初期化時は既存position/thesis gateを確認する。
4. 注文の種別（成行・指値・寄成）、休場日、次の立会日を確認する。休場日・立会時間外は `orders[].state: submitted` とし、約定価格を推定で埋めない（[`../anti-patterns.md`](../anti-patterns.md) AP-09）。

## Path と front matter

```text
records/04-position/YYYY/MM/YYYY-MM-DD-<ticker>.md
```

日付は最初の約定イベントの日。front matter の完全な形は `records/_schemas/position.json`（contract-of-record）を正とし、下は形を確認するための最小例。

```yaml
position_id: trade-YYYYMMDD-XXXX
ticker: "XXXX"
name: "..."
thesis_ref: records/03-thesis/YYYY/MM/YYYY-MM-DD-XXXX-<playbook_id>.md
position_state: none | open | closed
execution_state: none | submitted | broker_rejected | cancelled | expired | not_filled | partially_filled | filled
order_intent: { order_intent_id: intent-YYYYMMDD-XXXX-entry, side: buy | sell, quantity: 100, order_price_guard_yen: 1000, uses_margin: false }
orders: [ { order_id: order-YYYYMMDD-XXXX-1, origin_order_intent_id: intent-YYYYMMDD-XXXX-entry, side: buy, state: filled, submitted_quantity: 100, filled_quantity: 100 } ]
executions: [ { execution_id: exec-..., order_id: order-..., side: buy, quantity: 100, price_yen: 1000, at: "YYYY-MM-DDTHH:MM:SS+09:00" } ]
position_sizing_overlay: { estimated_real_order_notional_yen: 100000, guarded_max_notional_yen: 100000 }
review_valuation: { as_of: "YYYY-MM-DD", fair_value_yen: 1400, current_price_yen: 1050, valuation_zone: cheap | fair | rich, action: hold | add | sell }
kill_switch_check: { earnings_straddle: false, boj_eve: false, fomc_eve: false }
estimate_calibration: { entry_expected_upside_pct: 40.0, entry_expected_yield_pct: 0.0, realized_return_pct: null, realized_yield_pct: null, thesis_held: null }
```

portfolio資本は各positionへ複製せず、portfolio ledgerから再計算する。position recordは注文・約定・保有の銘柄別証拠を担当し、ledger eventのsourceとして参照される。

## State model

`execution_state` は執行意図（order_intent）の最終状態、`orders[].state` は個々の注文の状態、`position_state` は約定履歴から検証される建玉の状態。`orders[].origin_order_intent_id` は order_intent と一致させる。`orders[].filled_quantity <= submitted_quantity`。`position_state: none` の record は executions を持たない。

multi-intent lifecycleは [`../reference/execution-lifecycle.md`](../reference/execution-lifecycle.md) を正本にする。そこでは human-confirmed decision、manual broker order、broker-confirmed executionを別artifactとして持ち、intentの最大許容価格と各orderのlimit priceを分ける。現行position recordは`position.json`のcontractを使い、active recordへの切替はactive stateの再構成と同時に行う。

人間承認前の価格・数量案は[`../reference/decision-packet.md#execution-pricing`](../reference/decision-packet.md#execution-pricing)のexecution policyで作る。承認後だけproposalのpacket hashを#333 lifecycle intentへ束縛する。期限後のlow price touchはfillではないため、broker-confirmed executionと別にnot-filled outcomeとして測定する。

## 期限付き指値（約定待ち）の運用

指値に期限を付けて発注し約定を待つ場合（例: 月末まで有効の GTC 風注文）、record は次の形で管理する:

- **発注時**: `position_state: none`・`execution_state: submitted`・`orders[].state: submitted`・`executions: []` で record を作る。`order_intent.expires_at` に注文期限を入れる。約定価格を推定で埋めない（AP-09）。
- **期限内のイベント跨ぎ**: 期限までに FOMC・日銀会合・CPI 等を跨ぐ場合は、`macro_context_fit.sizing_caution` にその旨を残し、**約定前の撤回条件**（例: macro context の refresh trigger 発火・円の閾値割れ）を record 本文に明文化する。kill_switch_check は発注時点の判定であり、期限内イベントはこの撤回条件で管理する。
- **約定時**: `executions[]` / `entry_legs[]` を追記し、`orders[].state: filled`・`execution_state: filled`・`position_state: open`・`current_quantity` を更新する。thesis 側は書き換えない（entry 時の見積りを較正の基準として保存する）。
- **期限切れ・撤回時**: `orders[].state: expired | cancelled`・`execution_state: expired | cancelled` に更新し、ledgerへ`release`を記録する。releaseなしにreserved cashを暗黙解放しない。

## 買い・長期保有・押し目での買増し

- **買い**：research が確認した「割安ゾーン ∧ FV より十分に安い」で entry する。指値の上限価格と単元株数で数量を丸める。
- **長期保有**：株価の下落では切らない。含み損でも、塩漬け耐性が保たれている限り保有を続ける。
- **押し目での買増し**：保有銘柄がさらに割安なら、available cashとcurrent + reserved exposureをledgerで再計算して買い増す。warning超過は理由と期限を明示する。

## 割高で全売り

売りの引き金は 2 つだけ：**(a) 割高化**（現値が FV へ収束・割高ゾーン到達）、**(b) 事業のファンダメンタルズ毀損**（減益トレンド・財務悪化・減配・thesis の中核崩壊）。いずれの場合も **全株売却**する（部分売却はしない）。exit の `executions[]` と、手数料等控除前後のリターン・執行コストを記録する。

## 保有の見直しと見積りの較正

- **定例の見直し**：**月次**（積立と同じ周期）と **決算発表後**に、各保有の `review_valuation`（FV・現値・valuation zone・次の行動）を更新する。割高ゾーンに到達していれば全株売却、割安が続いていれば保有または買増し。決算後の見直しが必要な保有は GitHub Issue（`task:earnings-review` ラベル、`task: YYYY-MM-DD <ticker> を <event> 後に確認する`）で実行漏れを防ぐ。判断の正本は records に戻す。
- **見積りの較正（estimate calibration）**：exit 時と決算後に `estimate_calibration` を更新し、entry 時の見積り（想定上昇率・期待利回り）と実現結果（実際のリターン・利回り・thesis の的中）を突き合わせる。系統的なずれ（マクロの読み・FV 推定・耐性判定のどこが外れたか）を次の見積りに反映する（= 改善ループ、[`../doctrine.md`](../doctrine.md) 柱 3）。保有の対 benchmark 相対リターンは `uv run baibai-loop-position benchmark`（`1321` proxy、[`../reference/data-sources.md`](../reference/data-sources.md)）で機械的に算出し、較正の参考情報にする。

月次の下書きは read-only CLI で作る。

```bash
uv run baibai-loop-position calibration --asof YYYY-MM-DD
```

`calibration` は `records/04-position/**/*.md` の open position、対応する `records/03-thesis` の FV、J-Quants bars（`data/screening/market.sqlite`）を読み、保有ごとの entry 見積り・現在リターン・benchmark 相対リターン・FV gap・draft `valuation_zone` / `action` を YAML で出す。draft `valuation_zone` は FV がある場合だけ `cheap` / `fair` / `rich` を機械計算し、draft `action` は `rich` のとき `sell`、それ以外は `hold` を出す。これは `review_valuation` と `estimate_calibration` 更新の下書きであり、自動の exit 判断ではない。FV または J-Quants bars が欠ける項目は `null` として残し、CLI warning と coverage で欠損を確認する。

## Kill switch check

`kill_switch_check` は保有中の継続監視として記録する。ファンダメンタルズ毀損を検知したら「全売り (b)」で exit する。結果が二値に振れるイベント（決算跨ぎ・日銀会合前日・FOMC 前日）の直前の新規建玉は、避けるか小さくする（長期の積立では必須の禁止事項ではない、[`../portfolio-management.md`](../portfolio-management.md)）。

## AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| 注文 / 約定の記録・損益 / コストの計算・較正の集計 | ○ | |
| **実際の発注・取消・決済の操作** | | ○ |
| **exit 判断・kill switch の最終判定** | | ○ |

## Validation

```bash
uv run baibai-loop-validation --target position
uv run baibai-loop-validation --target ledger
uv run baibai-loop-validation
```

position validationは注文・約定の整合を、ledger validationはcash reconciliation、reservation lifecycle、保有数量、warning/overrideを検査する。

## 参考

- [`./research.md`](./research.md)：approved thesis と FV・見積り
- [`../portfolio-management.md`](../portfolio-management.md)：cap・kill switch・全売りの規律
- [`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)：資本eventとsnapshot式
- [`../doctrine.md`](../doctrine.md)：柱 3（見積りの較正）
- [`../anti-patterns.md`](../anti-patterns.md)：AP-09（会社 IR の一次確認・注文と約定の状態分離）
