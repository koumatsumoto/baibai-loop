# components/research.md

Baibai-Loop の **research / investment memo** の運用仕様。candidates × outlook × portfolio policy から選定した個別銘柄について、thesis、payoff、反証、entry / exit / invalidation、position size を検証する。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md)、概念モデルは [`../concepts.md`](../concepts.md) を参照。

## 1. 役割

- `records/04-candidates/` の pinned repository file から、深掘りする ticker / playbook を選ぶ
- `records/03-outlook/` と security exposure を使い、macro regime gate を確定する
- Portfolio policy と portfolio exposure file に照らして、採用可否と sizing を決める
- thesis payoff を構造化し、target / stop / expected upside / downside / risk reward / time horizon を検査する
- risk / contradicting evidence を必ず確認し、割安 trap を避ける
- Decision register と trade order intent へ接続する

## 2. 選定プロセス

1. 直近の `records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml` を読む
2. `playbook_screen_result`, `policy_gate_result`, `liquidity_gate_result`, `macro_regime_gate_result` を分けて確認する
3. `records/03-outlook/` の `sectors` / `exposure_buckets` と universe file の `security_exposures[]` を確認する
4. `candidate_evidence_decisions[]` で research recorded_at 時点の `effective_sizing_eligible` を再評価する
5. `selected_supporting_evidence_refs[]` に candidate / research の採用 evidence を明示する
6. thesis payoff と portfolio exposure file に照らし、`research_decision` を確定する

候補は一度の selection で 3-5 銘柄までに絞る。複数 playbook hit は優先度を上げる材料だが、sizing count には `effective_sizing_eligible` と `independence_component_id` の再評価後の値だけを使う。

`screening_selected` は research triage queue であり、採用判断ではない。`research_memo` は個別 investment memo が存在する状態、`research_approved` は `research_decision.outcome: approved`、`order_ready` は approved research と有効な order intent がそろった状態を指す。現在の投資方針は E2E regeneration の selected queue を正本にし、baseline で安定して出る 5 銘柄を core、liquidity stress で出る候補を execution-feasibility complement、sales-first でのみ増える single-evidence 候補を小さい exploration sleeve として扱う。evidence count 1 の候補は、反証 evidence と payoff を research で確認するまで大きく張らない。

## 3. Path と命名

```
records/05-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook_id>.md
```

## 4. Front Matter

Front matter の形は [`../templates/research.md`](../templates/research.md) を正とする。主な必須 field は次の通り。

- `ticker` / `name`
- `playbook_id` / `playbook_ref`
- `policy_ref`
- `policy_applicability`
- `portfolio_exposure_ref`
- `candidate_ref`
- `calendar_refs`
- `research_decision`
- `macro_regime_gate`
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
- `rejected`: thesis または gate が通らない

`posture` は `act_now | wait_for_event | wait_for_capital | dropped`。`approved` は `act_now` のみ。`rejected` は `rejection_reason`、`deferred` かつ待機姿勢の場合は `deferral_reason` と revisit 条件を持つ。

## 6. Macro Regime Gate

`macro_regime_gate.decision_effect` は `pass | conditional | block`。

- `pass`: policy と payoff 条件を満たせば採用可
- `conditional`: blocking condition または低 sizing cap を必須にする
- `block`: approved 不可

複数 macro input は reducer で合成し、record の値は validator が再計算値と一致検査する。unknown / expired / low confidence exposure は conservative に扱う。

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

Portfolio policy の minimum payoff を下回る場合は `policy_overrides[]` を要求する。Override 可否は policy rule で定義し、未定義 rule は default error とする。

## 9. Position Size

Position size は次の順で決める。

1. conviction tier の default sizing ladder
2. thesis payoff / minimum payoff
3. macro regime gate cap
4. event / calendar cap
5. liquidity cap
6. ticker / sector / playbook / economic exposure の cumulative cap
7. board lot と guard price による rounding

実注文数量は `floor(real_order_intent_yen / order_price_guard_yen / board_lot) * board_lot` で計算する。0 株になる場合は `trade_execution_state: none` と `not_submitted_reason` を記録する。

## 10. Body Sections

本文は playbook ごとの body schema に従う。共通の観点は次の通り。

1. Thesis
2. Macro regime gate
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

## 11. Validation

```bash
uv run baibai-loop-validate --target research
```

Validation は front matter schema、playbook body schema、repository refs、decision consistency、macro regime gate、evidence count、thesis payoff を検査する。

## 12. Trade / Review への接続

Approved memo は decision register に `decision_scope: research_memo` として記録される。実行する場合は `order_intent` を decision register に作り、`records/06-trades/` の `orders[].origin_order_intent_id` と join する。Outcome は review / retro で evidence、macro regime gate、sizing、execution、playbook へ帰属する。
