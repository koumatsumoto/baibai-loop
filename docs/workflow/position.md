---
title: "Workflow — position (execution & holding)"
summary: "売買執行記録：approved thesis の注文・約定・長期保有・押し目買増し・holding reviewを記録し、見積り（RR・期待利回り）と実現結果を突き合わせて較正する。"
doc_type: workflow
status: active
last_reviewed: 2026-07-11
---

# Workflow — 執行・保有（position）

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の執行・保有・較正の工程。[`./research.md`](./research.md) で `approved` になった判断について、注文・約定・長期保有・押し目での買増し・**holding review に基づく reduce / exit**を記録し、entry 時の見積り（リスクリワード・期待利回り・FV）を **実現結果と突き合わせて較正する**（= 運用の改善）。自動発注はしない。position record の契約正本は `records/_schemas/position.json`、holding review の契約正本は `records/_schemas/holding-review.json`。

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
review_valuation: { as_of: "YYYY-MM-DD", fair_value_yen: 1400, current_price_yen: 1050, valuation_zone: cheap | fair | rich, action: hold | add | sell } # #341移行までの旧field
kill_switch_check: { earnings_straddle: false, boj_eve: false, fomc_eve: false } # #341移行までの旧field
estimate_calibration: { entry_expected_upside_pct: 40.0, entry_expected_yield_pct: 0.0, realized_return_pct: null, realized_yield_pct: null, thesis_held: null }
```

portfolio資本は各positionへ複製せず、portfolio ledgerから再計算する。position recordは注文・約定・保有の銘柄別証拠を担当し、ledger eventのsourceとして参照される。

## State model

`execution_state` は執行意図（order_intent）の最終状態、`orders[].state` は個々の注文の状態、`position_state` は約定履歴から検証される建玉の状態。`orders[].origin_order_intent_id` は order_intent と一致させる。`orders[].filled_quantity <= submitted_quantity`。`position_state: none` の record は executions を持たない。

multi-intent lifecycleは [`../reference/execution-lifecycle.md`](../reference/execution-lifecycle.md) を正本にする。そこでは human-confirmed decision、manual broker order、broker-confirmed executionを別artifactとして持ち、intentの最大許容価格と各orderのlimit priceを分ける。現行position recordは`position.json`のcontractを使い、active recordへの切替はactive stateの再構成と同時に行う。

#341のactive lifecycle移行後だけ、人間承認前の価格・数量案を[`../reference/decision-packet.md#execution-pricing`](../reference/decision-packet.md#execution-pricing)のexecution policyで作る。承認後だけproposalのpacket hashを#333 lifecycle intentへ束縛し、期限後のlow price touchをbroker-confirmed executionと別にnot-filled outcomeとして測定する。移行前はactive `position.json` contractだけを使い、lifecycle、ledger、not-filled artifactを併設しない。

## 期限付き指値（約定待ち）の運用

指値に期限を付けて発注し約定を待つ場合（例: 月末まで有効の GTC 風注文）、record は次の形で管理する:

- **発注時**: `position_state: none`・`execution_state: submitted`・`orders[].state: submitted`・`executions: []` で record を作る。`order_intent.expires_at` に注文期限を入れる。約定価格を推定で埋めない（AP-09）。
- **期限内のイベント跨ぎ**: 期限までに FOMC・日銀会合・CPI 等を跨ぐ場合は、material deltaをwarningとしてproposalに残す。撤回はmacroの更新そのものではなく、個別仮説・価格根拠・永久損失評価を崩す新情報が出た場合だけにし、その条件をrecord本文に明文化する。kill_switch_checkは発注時点の判定であり、期限内イベントはこの撤回条件で管理する。
- **約定時**: `executions[]` / `entry_legs[]` を追記し、`orders[].state: filled`・`execution_state: filled`・`position_state: open`・`current_quantity` を更新する。thesis 側は書き換えない（entry 時の見積りを較正の基準として保存する）。
- **期限切れ・撤回時**: `orders[].state: expired | cancelled`・`execution_state: expired | cancelled` に更新する。#341移行後はledgerへ`release`を記録し、reserved cashを暗黙解放しない。移行前はactive `position.json` contractだけを更新する。

## 買い・長期保有・押し目での買増し

- **買い**：research が確認した「割安ゾーン ∧ FV より十分に安い」で entry する。指値の上限価格と単元株数で数量を丸める。
- **長期保有**：株価の下落では切らない。含み損でも、塩漬け耐性が保たれている限り保有を続ける。
- **押し目での買増し**：保有銘柄がさらに割安なら、#341移行後はavailable cashとcurrent + reserved exposureをledgerで再計算して買い増す。移行前は既存position / thesis gateを使う。warning超過は理由と期限を明示する。

## 売却（thesis break の全売り・代替優位の縮小）

売買判断は holding review（[`../reference/holding-review.md`](../reference/holding-review.md)）の `hold / add / reduce / exit` に従う。**thesis break（事業のファンダメンタルズ毀損：減益トレンド・財務悪化・減配・thesis の中核崩壊・verified な永久損失軸）は全株 exit** する。**FV 到達は review trigger** であって自動売却ではなく、税・費用を引いた代替候補が現保有を上回るときだけ reduce（部分）/ exit する。exit / reduce では `executions[]` と、手数料等控除前後のリターン・執行コストを記録する。

## 保有の見直しと見積りの較正

- **定例・event後の見直し**：保有確認は月次入金に強制されず、決算発表後またはmaterialな変化があった対象から holding review（thesis health・税引後代替）を更新する。判断式と算術は[`../reference/holding-review.md`](../reference/holding-review.md)、triggerと対象選択は[`../operations/decision-cycle.md#5-earnings-and-material-event-path`](../operations/decision-cycle.md#5-earnings-and-material-event-path)を正本とする。決算後の見直しが必要な保有は GitHub Issue（`task:earnings-review` ラベル、`task: YYYY-MM-DD <ticker> を <event> 後に確認する`）で実行漏れを防ぎ、判断の正本は records に戻す。
- **見積りの較正（estimate calibration）**：exit 時と決算後に `estimate_calibration` を更新し、entry 時の見積り（想定上昇率・期待利回り）と実現結果（実際のリターン・利回り・thesis の的中）を突き合わせる。系統的なずれ（マクロの読み・FV 推定・耐性判定のどこが外れたか）を次の見積りに反映する（= 改善ループ、[`../doctrine.md`](../doctrine.md) 柱 3）。portfolio全体の実績は `outcome` がJPX TOPIX配当込みと同期間で比較する。個別保有のprice-relative値はcalibration用diagnosticであり、総合収益率ではない。

下書きは read-only CLI で作る。

```bash
uv run baibai-loop-position calibration --asof YYYY-MM-DD
uv run baibai-loop-position holding-review --input records/04-position/YYYY/MM/YYYY-MM-DD-XXXX-review.yaml
```

`calibration` は `records/04-position/**/*.md` の open position、対応する `records/03-thesis` の FV、J-Quants bars（`data/screening/market.sqlite`）を読み、保有ごとの entry 見積り・現在リターン・benchmark 相対リターン・FV gap・draft `valuation_zone` / `review_trigger` を YAML で出す。draft `valuation_zone` は FV がある場合だけ `cheap` / `fair` / `rich` を機械計算し、`review_trigger` は **現値が FV に到達したとき** `true` を出す。これは **保有見直しが必要になったこと**を示す trigger であり、自動 sell ではない。`hold/add/reduce/exit` の判断は holding review が thesis health と税引後代替から行う。FV または J-Quants bars が欠ける項目は `null` として残し、CLI warning と coverage で欠損を確認する。

`holding-review` は holding review draft を読み、thesis health・現値/FV の review trigger・税引後代替から action を再計算して、記録された action との一致を検査する。契約の正本は[`../reference/holding-review.md`](../reference/holding-review.md)。

## Binary event 直前の新規建玉

結果が二値に振れるイベント（決算跨ぎ・日銀会合前日・FOMC 前日）の直前の新規建玉は、避けるか小さくする（長期の積立では必須の禁止事項ではない、[`../portfolio-management.md`](../portfolio-management.md)）。保有の売却判断は holding review の thesis health（永久損失軸）で行い、価格下落そのものは売却理由にしない。

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
uv run baibai-loop-validation --target holding-review
uv run baibai-loop-validation
```

position validationは注文・約定の整合を、ledger validationはcash reconciliation、reservation lifecycle、保有数量、warning/overrideを検査する。

## 参考

- [`./research.md`](./research.md)：approved thesis と FV・見積り
- [`../portfolio-management.md`](../portfolio-management.md)：cap・保有見直し・売却の規律
- [`../reference/holding-review.md`](../reference/holding-review.md)：thesis health と税引後代替の契約
- [`../reference/portfolio-ledger.md`](../reference/portfolio-ledger.md)：資本eventとsnapshot式
- [`../doctrine.md`](../doctrine.md)：柱 3（見積りの較正）
- [`../anti-patterns.md`](../anti-patterns.md)：AP-09（会社 IR の一次確認・注文と約定の状態分離）
