# components/research.md

Baibai-Loop の **research / investment memo** の運用仕様。candidates × macro context × portfolio policy から選定した個別銘柄について、thesis、payoff、反証、entry / exit / invalidation、position size を検証する。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md)、概念モデルは [`../concepts.md`](../concepts.md) を参照。

## 1. 役割

- `records/04-candidates/` の pinned repository file から、深掘りする ticker / playbook を選ぶ
- `records/01-macro-context/` と候補の sector / business context を使い、macro context fit を確認する
- Portfolio policy、liquidity、calendar / event risk に照らして、採用可否と sizing を決める
- thesis payoff を構造化し、target / stop / expected upside / downside / risk reward / time horizon を検査する
- 短期 thesis が外れた場合に長期保有へ切り替えられるか、balance sheet / cash flow / liquidity / refinancing risk / earnings base の耐久性を確認する
- AI 長期影響を、long-hold fallback の質を評価する strategic lens として機会・脅威の両面から確認する
- risk / contradicting evidence を必ず確認し、割安 trap を避ける
- Decision register と trade order intent へ接続する

## 2. 選定プロセス

1. 直近の `records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml` を読む
2. `playbook_screen_result`, `policy_gate_result`, `liquidity_gate_result` を分けて確認する
3. `records/01-macro-context/` の `sector_tilts` と候補の `sector_33` を確認する
4. `candidate_evidence_decisions[]` で research recorded_at 時点の `effective_sizing_eligible` を再評価する
5. `selected_supporting_evidence_refs[]` に candidate / research の採用 evidence を明示する
6. thesis payoff、portfolio policy、liquidity cap に照らし、`research_decision` を確定する

候補は一度の selection で 3-5 銘柄までに絞る。複数 playbook hit は優先度を上げる材料だが、sizing count には `effective_sizing_eligible` と `independence_component_id` の再評価後の値だけを使う。

`screening_selected` は research triage queue であり、採用判断ではない。`research_memo` は個別 investment memo が存在する状態、`research_approved` は `research_decision.outcome: approved`、`order_ready` は approved research と有効な order intent がそろった状態を指す。現在の投資方針は `baibai-loop-screening select` の `recommended_research_queue` を research 着手候補の正本にし、fast dislocation / core value / long-hold survivability のどの経路で拾われたかを `recommendation_queue` で確認する。evidence count 1 の候補は、反証 evidence と payoff を research で確認するまで大きく張らない。

## 3. Path と命名

```
records/05-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook_id>.md
```

## 4. Front Matter

Front matter の形は [`../templates/research.md`](../templates/research.md) を正とする。主な必須 field は次の通り。

- `ticker` / `name`
- `playbook_id` / `playbook_ref`
- `candidate_ref`
- `research_decision`
- `macro_context_ref`
- `macro_context_fit`
- `candidate_evidence_decisions`
- `selected_supporting_evidence_refs`
- `research_evidence_hits`
- `independent_evidence_count`
- `conviction_tier`
- `position_sizing_overlay`
- `thesis_payoff`

Repository ref は `ref_path` で判断時に参照した repo 内 file を指す。byte-level hash は持たせない。

`candidate_ref` は `candidates_ref`, `screen_run_id`, `ticker`, `candidate_id` を必須とする。
`screen_run_id` は参照先 candidates YAML の root `run_id` および candidate row の `screen_run_id`
と一致させる。形式は `screening-YYYYMMDD`。`candidate_id` は
`candidate-<asof_date>-<ticker>` 形式で、参照先 candidate row と一致させる。

## 5. research_decision

`research_decision.outcome` は次の 3 値。

- `approved`: 今すぐ採用してよい
- `deferred`: thesis は通るが、event / capital / regime / data gap で待つ
- `rejected`: thesis または必須確認が通らない

`posture` は `act_now | wait_for_event | wait_for_capital | dropped`。`approved` は `act_now` のみ。`rejected` は `rejection_reason`、`deferred` かつ待機姿勢の場合は `deferral_reason` と revisit 条件を持つ。

## 6. Macro Context Fit

`macro_context_fit.decision_effect` は `proceed | caution | defer`。

- `proceed`: policy と payoff 条件を満たせば採用可
- `caution`: 追加確認、低 sizing、event 待ちなどの条件を明示して採用可
- `defer`: macro context 上の前提が弱く、approved 不可

Macro context は hard gate ではないが、`defer` の場合は `research_decision.outcome: approved` にしない。unknown / stale / low confidence sector tilt は conservative に扱い、`required_checks[]` に追加確認を残す。

## 7. Evidence And Conviction

- `candidate_evidence_decisions[]` は candidate evidence の research recorded_at 時点の有効性判定
- `research_evidence_hits[]` は screen 後に確認した catalyst / freshness / risk / contradicting evidence
- `independent_evidence_count` は `effective_sizing_eligible: true` の distinct `independence_component_id` 数
- `sizing_eligible_evidence_family_count` は eligible evidence の atomic `evidence_family_set` union size
- Approved memo は少なくとも 1 件の selected supporting evidence と、1 件の risk / contradicting evidence review を持つ
- `source_status != ok` の evidence は sizing count に入れない

## 8. Thesis Payoff

Long-only の計算式は次で固定する。

- `expected_upside_pct = (target_price_yen / max_entry_price_yen - 1) * 100`
- `expected_downside_pct = (1 - stop_loss_yen / max_entry_price_yen) * 100`
- `risk_reward_ratio = expected_upside_pct / expected_downside_pct`
- `stop_loss_yen < max_entry_price_yen < target_price_yen`

Payoff が弱い場合は `research_decision`、`macro_context_fit.required_checks`、`macro_context_fit.sizing_caution`、および `position_sizing_overlay` に反映する。Policy の具体閾値は `policy_config.py` を正本とし、未実装 field を追加して補わない。

## 9. Position Size

Position size は次の順で決める。

1. conviction tier の default sizing ladder
2. thesis payoff / minimum payoff
3. macro context による caution / cap
4. event / calendar cap
5. liquidity cap
6. board lot と guard price による rounding

実注文数量は `floor(real_order_intent_yen / order_price_guard_yen / board_lot) * board_lot` で計算する。0 株になる場合は `trade_execution_state: none` と `not_submitted_reason` を記録する。

## 10. Body Sections

本文は playbook ごとの body schema に従う。共通の観点は次の通り。

1. Thesis
2. Macro context
3. Valuation snapshot
4. 一時的割安の原因仮説
5. 反対仮説
6. Catalyst
7. Price reaction
8. Positioning / liquidity
9. Shareholder return
10. Entry
11. Exit
12. Invalidation
13. Position size

`Thesis` には、短期 swing thesis に加えて以下を必ず 1 行以上で記録する。

- **Long-hold fallback**: 長期保有になっても耐えられる可能性が高い balance sheet、cash flow、流動性、借換リスク、収益基盤を確認し、含み損時に資産ロックを受け入れて長期保有へ切り替えられるかを明示する。固定年数の条件ではなく、売却までの期間が想定より長引いても事業継続性と回収余地が残るかを確認する。配当・自己株買い・安定 shareholder return がある場合は、資産ロック中の収益性として優先材料にできる。配当がない場合でも、短期リターン可能性と payoff が十分大きければ採用余地を残す。Long-hold fallback は stop loss、invalidation、kill switch、事業継続前提の毀損を上書きしない。
- **AI long-term impact**: AI の長期機会、長期脅威、今回判断での重みを明示する。AI 期待は単独の採用根拠、position sizing 根拠、macro context fit にはしない。

## 11. Validation

```bash
uv run baibai-loop-validate --target research
```

Validation は front matter schema、playbook body schema、repository refs、decision consistency、macro context fit、evidence count、thesis payoff を検査する。

## 12. Trade / Review への接続

Approved memo は decision register に `decision_scope: research_memo` として記録される。実行する場合は `order_intent` を decision register に作り、`records/06-trades/` の `orders[].origin_order_intent_id` と join する。Outcome は review / retro で evidence、macro context fit、sizing、execution、playbook へ帰属する。
