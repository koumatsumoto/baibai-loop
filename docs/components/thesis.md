# components/thesis.md

Baibai-Loop の **thesis / investment memo** の運用仕様。candidates × macro context × portfolio policy から選定した個別銘柄について、thesis、payoff、反証、entry / exit / invalidation、position size を検証する。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md)、概念モデルは [`../concepts.md`](../concepts.md) を参照。

## 1. 役割

- `records/04-candidates/` の pinned repository file から、深掘りする ticker / playbook を選ぶ
- `records/01-macro-context/` と候補の sector / business context を使い、macro context fit を確認する
- Portfolio policy、liquidity、event risk に照らして、採用可否と sizing を決める
- thesis payoff を構造化し、target / stop / expected upside / downside / risk reward / time horizon を検査する
- 短期 thesis が外れた場合に長期保有へ切り替えられるか、balance sheet / cash flow / liquidity / refinancing risk / earnings base の耐久性を確認する
- AI 長期影響を、long-hold fallback の質を評価する strategic lens として機会・脅威の両面から確認する
- risk / contradicting evidence を必ず確認し、割安 trap を避ける
- Decision register と trade order intent へ接続する

## 2. 選定プロセス

1. 最新 macro context と candidates に対して `baibai-loop-screening select` を実行し、`recommendations`、previous overlap、sector / playbook concentration を確認する。個別銘柄を掘るときは `ticker-profile --ticker XXXX` で事実 packet(相対モメンタム・次回決算日・規制 flag・直近 screening 記録)を起点にする
2. `recommendations` の上位 3-5 銘柄に絞る (閾値を試す場合は `records/_config/screening-rules/*.yaml` を直接編集して `select` を再実行)
3. 候補行の `evidence_hits[]`、candidate-level metrics、`records/01-macro-context/` の `sector_tilts` と `sector_33` を確認する
4. thesis payoff、portfolio policy、liquidity cap に照らし、`thesis_decision` と `position_sizing_overlay` を確定する

候補は一度の selection で 3-5 銘柄までに絞る。複数 playbook hit は優先度を上げる材料だが、sizing は payoff、liquidity、policy cap で決める。

`screening_selected` は `baibai-loop-screening select` の `recommendations` に入った状態であり、採用判断ではない。`research_memo` は個別 investment memo が存在する状態、`research_approved` は `thesis_decision.outcome: approved`、`order_ready` は approved research と有効な order intent がそろった状態を指す。

## 3. Path と命名

```
records/05-thesis/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook_id>.md
```

## 4. Front Matter

Front matter の形は [`../templates/thesis.md`](../templates/thesis.md) を正とする。主な必須 field は次の通り。

- `ticker` / `name`
- `playbook_id` / `playbook_ref`
- `candidate_ref`
- `thesis_decision`
- `macro_context_ref`
- `macro_context_fit`
- `position_sizing_overlay`
- `thesis_payoff`
- `corporate_action_check`

Repository ref は `ref_path` で判断時に参照した repo 内 file を指す。byte-level hash は持たせない。

`candidate_ref` は `candidates_ref`, `ticker` を必須とする(schema 検証)。candidates は git 外の local store のため、参照先ファイルとの cross-check は行わない(監査証跡を保持しない方針)。

## 5. thesis_decision

`thesis_decision.outcome` は次の 3 値。

- `approved`: 今すぐ採用してよい
- `deferred`: thesis は通るが、event / capital / regime / data gap で待つ
- `rejected`: thesis または必須確認が通らない

`posture` は `act_now | wait_for_event | wait_for_capital | dropped`。`approved` は `act_now` のみ。`rejected` は `rejection_reason`、`deferred` かつ待機姿勢の場合は `deferral_reason` と revisit 条件を持つ。

## 6. Macro Context Fit

`macro_context_fit.decision_effect` は `proceed | caution | defer`。

- `proceed`: policy と payoff 条件を満たせば採用可
- `caution`: 追加確認、低 sizing、event 待ちなどの条件を明示して採用可
- `defer`: macro context 上の前提が弱く、approved 不可

Macro context は hard gate ではないが、`defer` の場合は `thesis_decision.outcome: approved` にしない。unknown / stale / low confidence sector tilt は conservative に扱い、`required_checks[]` に追加確認を残す。

## 7. Thesis Payoff

Long-only の計算式は次で固定する。

- `expected_upside_pct = (target_price_yen / max_entry_price_yen - 1) * 100`
- `expected_downside_pct = (1 - stop_loss_yen / max_entry_price_yen) * 100`
- `risk_reward_ratio = expected_upside_pct / expected_downside_pct`
- `stop_loss_yen < max_entry_price_yen < target_price_yen`

Payoff が弱い場合は `thesis_decision`、`macro_context_fit.required_checks`、`macro_context_fit.sizing_caution`、および `position_sizing_overlay` に反映する。Policy の具体閾値は `position/policy.py` を正本とし、未実装 field を追加して補わない。

## 8. Position Size

Position size は次の順で決める。

1. thesis payoff / minimum payoff
2. macro context による caution / cap
3. corporate action check
4. liquidity cap
5. policy cap
6. board lot と guard price による rounding

実注文数量は `floor(real_order_intent_yen / order_price_guard_yen / board_lot) * board_lot` で計算する。0 株になる場合は `execution_state: none` と `not_submitted_reason` を記録する。

## 9. Body Sections

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

### 9.1 Entry preflight

全 playbook の `Entry` section には、発注直前の preflight を置く。2026-06-01 以降の `approved` research では、本文 checklist に加えて front matter の `entry_preflight` を validator-visible な正本として残す。`Price reaction` section がある playbook では詳細をそちらに書いてもよいが、採用判断へ接続する要約は必ず `Entry` に残す。

最小 front matter:

```yaml
entry_preflight:
  evaluated_on: "YYYY-MM-DD"
  market_relative_return_pct: 0.0
  sector_or_peer_relative_return_pct: 0.0
  macro_freshness: current
  market_regime:
    regime: risk_on_rally | risk_off_selloff | neutral_range | unknown
    benchmark_return_20d: 0.0
    benchmark_ticker: "1321"
    asof: "YYYY-MM-DD"
    eval_date: "YYYY-MM-DD"
  tactical_exposure_after_order:
    sector_33_pct: 0.0
    playbook_pct: 0.0
  near_term_catalyst: false
  action: proceed
  reason: "..."
```

必須観点:

| 観点 | 記録する内容 |
| --- | --- |
| Price window | 比較開始日、entry 判定日、使った終値 / 現在値の basis |
| Market baseline | Nikkei または TOPIX の close-to-close return |
| Sector / peer baseline | 原則は sector index。同じ basis で取れない場合は 3-5 社 peer basket。どちらも難しい場合は `not_checked` と理由 |
| Relative return | 候補銘柄と market / sector / peer の差 |
| Macro freshness | `macro_context_fit.context_freshness` と、stale / future の扱い |
| Market regime | `screening/regime.py` の判定（benchmark 20bd return から `risk_on_rally` / `risk_off_selloff` / `neutral_range`）。閾値は ±3 %。2026-06-17 以降の approved research では必須 |
| Exposure review | 追加予定 order を含めた同一 `sector_33` または `playbook_id` の open entry / guarded notional が、`tactical_real_budget_yen` の 50% を超えるか |
| Action | `proceed` / `starter` / `defer` / `exception` のいずれか |

軽量な action rule:

- 候補銘柄が market baseline または sector / peer baseline に 3pt 以上劣後し、明確な near-term catalyst がない場合は、`starter` または `defer` を基本にする。
- `macro_context_fit.context_freshness: stale` で event-driven thesis ではない場合は、`defer` を基本にする。`exception` を使う場合は `exception_basis` に `near_term_catalyst` / `low_sizing` / `low_correlation` のいずれかを構造化して残す。
- 同一 sector または同一 playbook が tactical budget の 50% を超える exposure review trigger は hard cap ではない。既存 validator の real capital cap とは別に、opportunity cost / thesis overlap を確認するための手動 review trigger として扱う。低相関理由や catalyst 差を説明できない場合は、追加 entry を `starter` に抑えるか `defer` する。
- `market_regime.regime: risk_on_rally` のとき、逆張りバリュー entry は trending index に構造的に劣後する（`reports/2026-06-17-trade-strategy-rootcause.md` の root-cause analysis、backtest-runbook §6 の dated 計測 index）。`proceed` は禁止（hard trigger）。`starter` / `exception` でも `near_term_catalyst: true` または `exception_basis: [low_correlation]` がなければ validator warning（`thesis.entry-preflight-rally-contrarian`）が出て、`defer` を促す。
- validator は 2026-06-01 以降の approved research で、3pt 以上の相対劣後、stale macro、tactical exposure 50% 超、`risk_on_rally` regime を理由なし `proceed` として通さない。`exception` は `exception_basis` がない場合は通さない。2026-06-17 以降は `entry_preflight.market_regime` 自体が必須。

`Thesis` には、短期 swing thesis に加えて以下を必ず 1 行以上で記録する。

- **Long-hold fallback**: 長期保有になっても耐えられる可能性が高い balance sheet、cash flow、流動性、借換リスク、収益基盤を確認し、含み損時に資産ロックを受け入れて長期保有へ切り替えられるかを明示する。固定年数の条件ではなく、売却までの期間が想定より長引いても事業継続性と回収余地が残るかを確認する。配当・自己株買い・安定 shareholder return がある場合は、資産ロック中の収益性として優先材料にできる。配当がない場合でも、短期リターン可能性と payoff が十分大きければ採用余地を残す。Long-hold fallback は stop loss、invalidation、kill switch、事業継続前提の毀損を上書きしない。
- **AI long-term impact**: AI の長期機会、長期脅威、今回判断での重みを明示する。AI 期待は単独の採用根拠、position sizing 根拠、macro context fit にはしない。

## 10. Validation

```bash
uv run baibai-loop-validation --target thesis
```

Validation は front matter schema、playbook body schema、repository refs、decision consistency、macro context fit、corporate action check、thesis payoff、position sizing overlay を検査する。

## 11. Trade / Review への接続

Approved memo は decision register に `decision_scope: research_memo` として記録される。実行する場合は `order_intent` を decision register に作り、`records/06-position/` の `orders[].origin_order_intent_id` と join する。Outcome は review / retro で evidence、macro context fit、sizing、execution、playbook へ帰属する。
