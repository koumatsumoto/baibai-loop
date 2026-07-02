---
title: "Workflow — research (thesis)"
summary: "個別銘柄リサーチ：candidates と macro context から、フェアバリュー・リスクリワード・期待利回りを見積もり、塩漬け耐性を確認し、採否と sizing を決める投資メモ。"
doc_type: workflow
status: active
last_reviewed: 2026-07-01
---

# Workflow — 個別銘柄リサーチ（thesis）

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の中核。[`./screening.md`](./screening.md) の candidates と [`./macro.md`](./macro.md) の環境読みから個別銘柄を深く調べ、**フェアバリュー（FV）・リスクリワード・期待利回りを見積もり**、**塩漬け耐性**を確認して、採否と sizing を決める投資メモ（`records/05-thesis/`）。運用が磨く中核スキル＝この見積りの精度で、見積りは実現結果と突き合わせて calibrate する（[`./position.md`](./position.md)）。

契約の正本は `records/_schemas/thesis.json`（front matter の形・必須・enum）。本 doc は JSON に書けないもの（見積りの式・enum の意味・WHY・手順）を持つ。front matter の完全形は template でなく schema と実 record を正とし、本 doc 末尾に最小例を置く。

## 選定プロセス

1. 最新 macro context と candidates に対し `uv run baibai-loop-screening select` を実行し、`recommendations`・durability annotation・sector / playbook concentration を確認する。個別は `ticker-profile --ticker XXXX` の事実 packet を起点にする。
2. `recommendations` の上位 3–5 銘柄に絞る（閾値を試すなら `records/_config/screening-rules/*.yaml` を編集して `select` 再実行）。
3. 候補の `evidence_hits[]`・candidate metrics・macro context の `sector_tilts` を確認する。
4. thesis payoff・[`../portfolio-management.md`](../portfolio-management.md) の cap・耐性ゲート・liquidity に照らし、`thesis_decision` と `position_sizing_overlay` を確定する。

一度の selection で 3–5 銘柄まで。複数 playbook hit は優先度を上げる材料だが、sizing は payoff・耐性・liquidity・policy cap で決める。

## thesis_decision

`outcome` は 3 値：**`approved`**（今すぐ採用してよい）／**`deferred`**（thesis は通るが event / capital / data gap で待つ）／**`rejected`**（thesis または必須確認が通らない）。`posture` は `act_now | wait_for_event | wait_for_capital | dropped`（`approved` は `act_now` のみ）。`rejected` は `rejection_reason`、待機 `deferred` は `deferral_reason` と revisit 条件を持つ。

## Macro context fit

`decision_effect` は `proceed | caution | defer`。macro context は hard gate ではないが、`defer` の場合は `approved` にしない。unknown / stale / low-confidence の sector tilt は conservative に扱い、`required_checks[]` に追加確認を残す。sector tilt が headwind でも自動却下せず、`sizing_caution` / `required_checks` として扱う（[`./macro.md`](./macro.md)）。

## 見積り — フェアバリュー・リスクリワード・期待利回り

割安/割高は「機械 percentile ゾーン × 個別 FV」の二段で判断する（[`../portfolio-management.md`](../portfolio-management.md)）。

- **フェアバリュー（FV）**：配当割引 / 利益正常化 / 清算価値のいずれか（複数併記可）で 1 株あたり FV を推定し、根拠と前提を残す。**利益正常化はサイクル通期（例: J-Quants 5 期）で行い、ピーク / 循環高値の EPS を外挿しない**（ピーク EPS で FV を張ると upside と RR が水増しされる）。
- **買いの条件**：機械 valuation ranking の割安ゾーン ∧ 現値が FV に対して下方乖離。
- **payoff（long-only、schema `thesis_payoff`）**。ここでの RR は「FV upside 対 資産床までの margin of safety」であり、短期 exit の trade R:R ではない：
  - `expected_upside_pct = (fair_value_yen / max_entry_price_yen − 1) × 100`
  - `expected_downside_pct`：保守的な下値までの下方。**net-cash / 清算価値を床に使うのは、還元・実現機構（自己株買い・DOE・アクティビスト・清算パス）が確認できる場合に限る**。機構が無ければ床扱いにせず、percentile 追加ドローダウンや peer の trough 倍率など保守的な下値を使う（還元機構なしの net-cash は value trap で滞留し得る）。価格 stop は置かない。
  - `risk_reward_ratio = expected_upside_pct / expected_downside_pct`。**最低採用ラインは RR ≥ 2 を目安に、希望的 alpha を剥がした後の期待値が正**であること。
  - **期待利回り**：FV 収束の期待リターン（想定収束年数で年率化）に配当利回り（income）を加えた total-return の年率概算。income-only の配当利回りとは別に記録する。
- **売りの条件**：現値が FV へ収束（割高化）または割高ゾーン到達で **全売り**。期間では売らない。

payoff が弱い場合は `thesis_decision`・`macro_context_fit.required_checks`・`sizing_caution`・`position_sizing_overlay` に反映する。最低 payoff の下限方針は [`../portfolio-management.md`](../portfolio-management.md)。

## 塩漬け耐性ゲート（必須）

価格 stop を置かない前提を成立させるため、**採用候補は塩漬け耐性を必須で確認する**：net-cash または健全な balance sheet・営業 CF 黒字・低い有利子負債と借換リスク・耐久的な収益基盤。これらを満たさない割安は採用しない。配当・自己株買い・安定 shareholder return は資産ロック中の収益として加点材料（配当は必須ゲートではない）。front matter の `durability_gate` に記入し、合否判定は人間が行う。

## 原因仮説 ＋ 反対仮説（必須）

- **割安の原因仮説**：市場全体の売り／業種ローテーション／一過性の悪材料／投資先行での見栄え悪化／net-cash・資産・CF 創出力の見落とし／需給。
- **反対仮説（構造的理由、全 packet 必須で最低 1 件）**：(1) 構造的成長鈍化 (2) ガバナンス懸念 (3) 技術的陳腐化 (4) accounting 警戒 (5) 業界需要の構造的縮小 (6) ESG / 規制リスク (7) 大株主の売り圧力 (8) 営業 CF の一過性要因 (9) 有利子負債・偶発債務 (10) その他。これが「割安 trap でなく本物の割安か」「塩漬けに耐えるか」の中核チェック。

## 4 軸評価（単一総合点に戻さない）

各軸に **寄与度 3 段階**（strong / weak / neutral）を記録し、合計点は算出しない。

| 軸 | 評価対象 |
| --- | --- |
| Valuation | PER / PBR / EV-EBITDA / P-S / PCFR / OCF yield / net-cash と FV 乖離 |
| Durability（塩漬け耐性） | balance sheet / 営業 CF / 負債・借換 / 収益基盤の耐久性 |
| Catalyst | 有無 / freshness / 種別（一次ソース URL） |
| Positioning / liquidity | 空売り・信用・特別注意・貸借・出来高・ADV |

単一 score に畳むと「なぜ選んだか」を失い calibration の学習信号を劣化させるため、軸別のまま残す。

## Entry

- **買いは割安ゾーン ∧ FV 下方乖離**を満たす銘柄を長期で積み立てる。押し目（recent decline で割安ゾーンへ入った）を拾ってよい。
- **Entry preflight**（front matter `entry_preflight`）：比較開始日 / 判定日 / price basis、market（Nikkei/TOPIX）・sector 相対リターン（情報）、macro freshness、追加 order を含めた同一 sector / playbook の exposure review。sector baseline は原則 sector index を使い、同一 basis で取れなければ 3–5 社の peer basket、いずれも不可なら `not_checked` と理由を記録する。リスクオン相場での逆張りを禁じる regime trigger は持たない（割安を買うのが本流のため）。`action`（`proceed` / `starter` / `defer`）は、macro freshness が stale で event-driven でなければ `defer`、exposure が cap 近傍なら `starter`、それ以外は `proceed` で駆動する。
- sizing は §Position size に従う。

## Exit

- **全売りトリガーは 2 つだけ**：(a) 割高化（FV 到達・割高ゾーン）、(b) 事業の fundamental 毀損。価格の逆行では売らない（price-stop 撤廃）。時間では売らない（time stop なし）。
- **(b) の具体条件は銘柄ごとに事前列挙する**（`thesis_payoff.invalidation_conditions[]`）：塩漬け耐性の基盤（営業 CF・balance sheet・還元・収益基盤）が何をもって崩れたと判断するかを、この銘柄の数値・事象で定義する。反対仮説 10 類型（購入前の trap 判定）とは別に、保有中に監視する毀損条件を書く。
- exit の実行と見積り calibration は [`./position.md`](./position.md)。

## AI long-term impact

AI を中心セクターに据える思想は [`../doctrine.md`](../doctrine.md) 柱 2 が正本。thesis では当該銘柄の **AI の長期機会・長期脅威・今回判断での重み** を 1 行以上で明示する。AI 期待は単独の採用根拠・sizing 根拠にはしない。

## Position size

順に決める：(1) thesis payoff / 最低 payoff、(2) 塩漬け耐性、(3) macro caution / cap、(4) liquidity cap、(5) policy cap（[`../portfolio-management.md`](../portfolio-management.md)：ticker 4–6% / sector 30–40% / playbook 35%、entry 時 sizing 制約）、(6) board lot と guard price で丸め。0 株になる場合は `execution_state: none` と理由を記録する。

## AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| thesis / FV 見積り / 仮説ドラフト / catalyst / valuation snapshot / positioning 取得 | ○ | |
| macro context の前提確認 / 一次ソース URL 確認 | | ○ |
| 会社 IR の一次確認・最終採用判定・失敗分類確定 | | ○ |

価格や valuation percentile が極端な銘柄は `corporate_action_check` で株式分割・併合・合併の可能性を確認する（AP-03）。会社 IR（決算短信・説明資料・Q&A・有報 / 統合報告書・中計・還元開示）を一次情報として確認し、未確認のまま `approved` にしない（[`../anti-patterns.md`](../anti-patterns.md) AP-03 / AP-09）。

## front matter 最小例（完全形は `records/_schemas/thesis.json`）

```yaml
ticker: "XXXX"
name: "..."
playbook_id: cashflow-yield-discount
playbook_ref: { ref_path: records/_playbooks/<archetype>/<version>.md }
candidate_ref: { candidates_ref: records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml, ticker: "XXXX" }
macro_context_ref: records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml
macro_context_fit: { context_freshness: current, fit: neutral, decision_effect: proceed, required_checks: [], sizing_caution: [] }
thesis_decision: { outcome: approved, posture: act_now }
thesis_payoff:
  max_entry_price_yen: 1000
  fair_value_yen: 1400
  expected_upside_pct: 40.0
  expected_downside_pct: 15.0
  risk_reward_ratio: 2.67
  expected_yield_pct: 12.0            # total-return 年率概算（income + FV 収束）
  invalidation_conditions: ["営業CF 2 期連続赤字", "減配", "純有利子負債への転落"]
durability_gate: { net_cash: true, operating_cf_positive: true, low_leverage: true, refinancing_risk: low, dividend: true, judgment: high }
corporate_action_check: { checked: true, result: none, note: "" }
position_sizing_overlay: { estimated_real_order_notional_yen: 400000, guarded_max_notional_yen: 400000, adv_participation_pct: 1.2 }
sector_33: "情報・通信業"
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
```

（field 名・必須・enum は `records/_schemas/thesis.json` を正本とする。上は形の確認用。）

## Validation

```bash
uv run baibai-loop-validation --target thesis
```

front matter schema・repository refs・macro context fit・thesis payoff・durability_gate・corporate action check・position sizing overlay を検査する。

## 参考

- [`./screening.md`](./screening.md)：起点の candidates
- [`./position.md`](./position.md)：執行・全売り・見積り calibration
- [`./playbooks.md`](./playbooks.md)：割安 value の archetype
- [`../portfolio-management.md`](../portfolio-management.md)：cap・耐性ゲート・最低 payoff
- [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)：valuation 指標
