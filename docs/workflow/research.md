---
title: "Workflow — research (thesis)"
summary: "個別銘柄リサーチ：candidates と macro context から、フェアバリュー・リスクリワード・期待利回りを見積もり、塩漬け耐性を確認し、採否と投入額を決める投資メモ。"
doc_type: workflow
status: active
last_reviewed: 2026-07-02
---

# Workflow — 個別銘柄リサーチ（thesis）

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の中核工程。[`./screening.md`](./screening.md) の candidates と [`./macro.md`](./macro.md) の環境認識を材料に個別銘柄を深く調べ、**フェアバリュー（FV）・リスクリワード・期待利回りを見積もり**、**塩漬け耐性**を確認したうえで、採否と投入額を決める投資メモ（`records/03-thesis/`）を書く。運用で磨く中核の技能はこの見積りの精度であり、見積りは実現結果と突き合わせて較正する（[`./position.md`](./position.md)）。

契約の正本は `records/_schemas/thesis.json`（front matter の形・必須項目・enum）。本 doc は JSON に書けないもの（見積りの式・enum の意味・設計判断の理由・手順）を持つ。front matter の完全な形は template ではなく schema と実際の record を正とし、本 doc 末尾に最小限の例を置く。

## 選定プロセス

1. 最新の macro context と candidates に対して `uv run baibai-loop-screening select` を実行し、`recommendations`（機械 E[r] 降順）・E[r] 成分と FV アンカー・durability（塩漬け耐性）の注記・sector / playbook ごとの集中度を確認する。個別銘柄は `ticker-profile --ticker XXXX` の事実 packet を起点にする。
2. `recommendations` の上位 3–5 銘柄に絞る（閾値を変えて試すときは `records/_config/screening-rules/*.yaml` を編集して `select` を再実行する）。
3. 各候補の `evidence_hits[]`・candidates の指標・macro context の `sector_tilts` を確認する。
4. リスクリワード・[`../portfolio-management.md`](../portfolio-management.md) の cap・塩漬け耐性ゲート・流動性に照らして、`thesis_decision` と `position_sizing_overlay` を確定する。

一度の選定で扱うのは 3–5 銘柄まで。複数の playbook に同時に該当することは優先度を上げる材料になるが、投入額はリスクリワード・耐性・流動性とportfolio exposureで決める。canonical ledger稼働時はavailable cashとexposure warning、未初期化時は既存position/thesis gateを使う。

## thesis_decision

`outcome` は 3 値：**`approved`**（今すぐ採用してよい）／**`deferred`**（thesis 自体は通るが、イベント待ち・資金待ち・データ不足で保留）／**`rejected`**（thesis または必須確認が通らない）。`posture` は `act_now | wait_for_event | wait_for_capital | dropped`（`approved` は `act_now` のみ）。`rejected` には `rejection_reason`、保留の `deferred` には `deferral_reason` と再検討の条件（revisit）を必ず付ける。

## Macro context fit

`decision_effect` は `proceed | caution | defer`。macro context は機械的な足切りではないが、`defer` の場合は `approved` にしない。判定不能・鮮度切れ・確信度の低い sector tilt は保守的に扱い、`required_checks[]` に追加確認を残す。sector tilt が逆風（headwind）でも自動的に却下はせず、`sizing_caution` / `required_checks` として扱う（[`./macro.md`](./macro.md)）。

## 見積り — フェアバリュー・リスクリワード・期待利回り

割安 / 割高は「機械的な percentile ゾーン × 個別のフェアバリュー」の二段で判断する（[`../portfolio-management.md`](../portfolio-management.md)）。

- **フェアバリュー（FV）**：配当割引・利益の正常化・清算価値のいずれか（複数併記可）で 1 株あたりの適正価値を推定し、根拠と前提を残す。**select が転記する機械アンカー（`fv_sector_median_yen` / `fv_self_range_yen`・E[r] 成分）を出発点にし、手動 FV が機械アンカーから大きく乖離する場合はその理由（正常化前提・sector 比較の不適合・還元強化など）を本文に書く**（見積り較正で機械 vs 手動 vs 実現の三者を突き合わせられるようにする）。**利益の正常化は景気サイクルの通期（例: J-Quants 5 期分）で行い、ピーク益や循環的な高値の EPS をそのまま外挿しない**（ピーク益で FV を張ると、想定上昇率とリスクリワードが水増しされる）。
- **買いの条件**：機械的な valuation ranking の割安ゾーンにあり、かつ現値が FV より十分に安いこと。
- **payoff（買い建てのみ、schema `thesis_payoff`）**。ここでのリスクリワード（RR）は「FV までの上昇余地 対 資産の裏付けまでの安全余裕」であり、短期売買の損益比ではない：
  - `expected_upside_pct = (fair_value_yen / max_entry_price_yen − 1) × 100`
  - `expected_downside_pct`：保守的に見た下値までの下落率。**ネットキャッシュや清算価値を下値の床として使えるのは、還元・価値実現の仕組み（自社株買い・DOE・アクティビスト・清算への道筋）が確認できる場合に限る**。仕組みがなければ床とはみなさず、percentile の追加下落余地や同業他社の底値倍率など、保守的な下値を使う（還元の仕組みを欠くネットキャッシュはバリュートラップとして滞留し得る）。価格による損切りは置かない。
  - `risk_reward_ratio = expected_upside_pct / expected_downside_pct`。**採用の目安は RR ≥ 2、かつ希望的な超過収益を差し引いた後でも期待値がプラス**であること。
  - **期待利回り**：FV への収束で得られる期待リターン（想定する収束年数で年率換算）に配当などの収益を加えた、トータルリターンの年率概算。配当利回り単体とは別に記録する。
- **売りの条件**：現値が FV へ収束（割高化）するか、割高ゾーンに到達したら **全株売却**。保有期間の長さでは売らない。

payoff が弱い場合は `thesis_decision`・`macro_context_fit.required_checks`・`sizing_caution`・`position_sizing_overlay` に反映する。リスクリワードの下限方針は [`../portfolio-management.md`](../portfolio-management.md)。

## 塩漬け耐性ゲート（必須）

価格による損切りを置かない前提を成立させるため、**採用候補には塩漬け耐性の確認を必須で課す**：ネットキャッシュまたは健全なバランスシート・営業キャッシュフローの黒字・低い有利子負債と借換リスク・耐久性のある収益基盤。これらを満たさない割安は採用しない。配当・自社株買い・安定した株主還元は、資金が拘束されている間の収益として加点材料にする（配当は必須の関門ではない）。front matter の `durability_gate` に記入し、合否の判定は人間が行う。

## 原因仮説 ＋ 反対仮説（必須）

- **割安の原因仮説**：市場全体の売り／業種のローテーション／一過性の悪材料／先行投資による見かけの悪化／ネットキャッシュ・資産・キャッシュ創出力の見落とし／需給。
- **反対仮説（構造的な理由。すべての投資メモで最低 1 件）**：(1) 構造的な成長鈍化 (2) ガバナンス懸念 (3) 技術の陳腐化 (4) 会計への警戒 (5) 業界需要の構造的縮小 (6) ESG / 規制リスク (7) 大株主の売り圧力 (8) 営業キャッシュフローの一過性要因 (9) 有利子負債・偶発債務 (10) その他。これが「バリュートラップではなく本物の割安か」「塩漬けに耐えられるか」を見極める中核のチェックになる。

## 4 軸評価（単一の総合点に戻さない）

各軸に **寄与度 3 段階**（strong / weak / neutral）を記録し、合計点は算出しない。

| 軸 | 評価対象 |
| --- | --- |
| Valuation | PER / PBR / EV-EBITDA / P-S / PCFR / OCF yield / ネットキャッシュと FV からの乖離 |
| Durability（塩漬け耐性） | バランスシート / 営業キャッシュフロー / 負債・借換 / 収益基盤の耐久性 |
| Catalyst（株価修正の契機） | 有無 / 鮮度 / 種別（一次ソースの URL 付き） |
| Positioning / liquidity | 空売り・信用・特別注意・貸借・出来高・平均売買代金 |

単一のスコアに畳むと「なぜ選んだか」の情報が失われ、見積り較正の学習材料が劣化する。軸別のまま残す。

## Entry

- **買うのは、割安ゾーンにあり、かつ FV より十分に安い**銘柄。長期の積立として買い、押し目（直近の下落で割安ゾーンへ入った局面）を拾ってよい。
- **Entry preflight**（front matter `entry_preflight`）：比較開始日・判定日・価格の基準、市場（日経 / TOPIX）と sector に対する相対リターン（参考情報）、macro context の鮮度を確認する。canonical ledger稼働時のportfolio exposureはcurrent holdingとreservationから再計算しwarningとして提示する。未初期化時は既存position/thesis gateを使う。sector の基準値は原則 sector 指数を使い、取得できなければ同業 3–5 社の平均、それも不可なら`not_checked`と理由を記録する。macro contextが鮮度切れで日付の確定した近接カタリストもなければ`defer`とする。
- 投入額は §Position size に従う。

## Exit

- **全株売却の引き金は 2 つだけ**：(a) 割高化（FV 到達・割高ゾーン入り）、(b) 事業のファンダメンタルズ毀損。株価の下落そのものでは売らない（価格による損切りを置かない）。保有期間の長さでも売らない。
- **(b) の具体条件は銘柄ごとに事前に列挙する**（`thesis_payoff.invalidation_conditions[]`）：塩漬け耐性の土台（営業キャッシュフロー・バランスシート・株主還元・収益基盤）が「何をもって崩れたと判断するか」を、この銘柄の数値・出来事として定義する。反対仮説 10 類型（購入前のバリュートラップ判定）とは別に、保有中に監視する毀損条件を書く。
- exit の実行と見積りの較正は [`./position.md`](./position.md)。

## AI の長期影響

AI を中心セクターに据える思想は [`../doctrine.md`](../doctrine.md) 柱 2 が正本。thesis では、その銘柄にとっての **AI の長期的な機会・長期的な脅威・今回の判断における重み**を 1 行以上で明示する。AI への期待は、それ単独では採用理由にも投入額の根拠にもしない。

## Position size

次の順に確認して投入額を決める：(1) payoff とリスクリワードの下限、(2) 塩漬け耐性、(3) macro 起因の注意（sizing_caution）と cap、(4) 流動性の上限（ADV 参加率）、(5) policy cap（[`../portfolio-management.md`](../portfolio-management.md)：単一銘柄 4–6% / sector 30–40% / playbook 35%、entry 時の投入額に対する制約）、(6) 単元株数と指値の上限価格での丸め。丸めた結果 0 株になる場合は `execution_state: none` と理由を記録する。

## AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| thesis / FV 見積り / 仮説の下書き / カタリスト調査 / valuation snapshot / 需給情報の取得 | ○ | |
| macro context の前提確認 / 一次ソース URL の確認 | | ○ |
| 会社 IR の一次確認・最終的な採用判定・失敗分類の確定 | | ○ |

株価や valuation percentile が極端な銘柄は、`corporate_action_check` で株式分割・併合・合併の可能性を確認する（AP-03）。会社 IR（決算短信・説明資料・質疑応答・有価証券報告書 / 統合報告書・中期計画・還元の開示）を一次情報として確認し、未確認のまま `approved` にしない（[`../anti-patterns.md`](../anti-patterns.md) AP-03 / AP-09）。

## front matter 最小例（完全形は `records/_schemas/thesis.json`）

```yaml
ticker: "XXXX"
name: "..."
playbook_id: cashflow-yield-discount
playbook_ref: { ref_path: records/_playbooks/<archetype>/<version>.md }
candidate_ref: { candidates_ref: records/02-candidates/YYYY/MM/YYYY-MM-DD.yaml, ticker: "XXXX" }
macro_context_ref: records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml
macro_context_fit: { context_freshness: current, fit: neutral, decision_effect: proceed, required_checks: [], sizing_caution: [] }
thesis_decision: { outcome: approved, posture: act_now }
thesis_payoff:
  max_entry_price_yen: 1000
  fair_value_yen: 1400
  expected_upside_pct: 40.0
  expected_downside_pct: 15.0
  risk_reward_ratio: 2.67
  expected_yield_pct: 12.0            # トータルリターン年率概算（income + FV 収束）
  invalidation_conditions: ["営業CF 2 期連続赤字", "減配", "純有利子負債への転落"]
durability_gate: { net_cash: true, operating_cf_positive: true, low_leverage: true, refinancing_risk: low, dividend: true, judgment: high }
corporate_action_check: { checked: true, result: none, note: "" }
position_sizing_overlay: { estimated_real_order_notional_yen: 400000, guarded_max_notional_yen: 400000, adv_participation_pct: 1.2 }
sector_33: "情報・通信業"
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
```

（field 名・必須項目・enum は `records/_schemas/thesis.json` を正本とする。上は形を確認するための例。）


> **本文の必須セクション**: research memo の本文見出しは playbook ごとの `records/_playbooks/<playbook_id>/body-schema.yaml` が正本で、validator が欠落を error にする。新規作成時は同 playbook の直近の approved memo を雛形にすると欠落しない。

## Validation

```bash
uv run baibai-loop-validation --target thesis
```

front matter の schema・repository refs・macro context fit・thesis payoff・durability_gate・corporate action check・position sizing overlay を検査する。

## 参考

- [`./screening.md`](./screening.md)：起点となる candidates
- [`./position.md`](./position.md)：執行・全売り・見積り calibration
- [`./playbooks.md`](./playbooks.md)：割安 value の型（archetype）
- [`../portfolio-management.md`](../portfolio-management.md)：cap・耐性ゲート・リスクリワード下限
- [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)：valuation 指標
