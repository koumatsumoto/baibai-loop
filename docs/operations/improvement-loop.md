---
title: "改善ループ runbook"
summary: "基盤改善サイクルの正本。現状計測 → 仮説の事前登録 → design/confirm 検証 → 採用実装 → 運用テスト → 継続監視を、誠実性規律つきで回す手順。"
doc_type: operation
status: active
last_reviewed: 2026-07-23
related_docs:
  - "../doctrine.md"
  - "../reference/estimate-calibration.md"
  - "./decision-cycle.md"
---

# 改善ループ runbook — 見積り精度の継続改善

基盤（マクロ読み・screening 選定・E[r]/FV/RR 見積り）の精度を計測で改善するサイクルの正本。ゴールは「**マクロ経済分析・screening からの個別銘柄提案・期待値計算の精度を高め、お買い得銘柄をより適切に選定できる状態を作る**」こと。思想上の位置づけ（計測ファースト・誠実性の規律）は [`../doctrine.md`](../doctrine.md) 柱 5、計測基盤の実装仕様は [`../reference/estimate-calibration.md`](../reference/estimate-calibration.md) が正本。操作のskillは[`improvement-loop`](../../.agents/skills/improvement-loop/SKILL.md)。

## 改善対象マップ（レバーの所在）

改善仮説を立てるとき、どのレバーがどこにあるかをこの表で引く。

| レバー | 所在 | 計測経路 |
| --- | --- | --- |
| screen の閾値・gate・evidence pattern 条件 | `method/screening-rules/*.yaml` | 較正リプレイ（rules variant） |
| select の順位付け・diversity cap | 同上 + `src/baibai_engine/screening/selection/` | 較正リプレイ（selection replay） |
| 機械 E[r]・FV アンカー（anchor・実現率・cap・carry） | `src/baibai_engine/screening/estimates.py` | 較正リプレイ（er 軸 IC / decile / 予測 vs 実現） |
| valuation 指標の算出 | `src/baibai_engine/screening/metrics` 系 + [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md) | 較正リプレイ（軸別 IC / coverage） |
| マクロ読みの手順・レンズ | [`../workflow/macro.md`](../workflow/macro.md) + skill `macro-analysis` | 保有 outcome / 月次の事後検証（N≈1、統計計測はしない） |
| research の見積り手順（FV・RR・耐性） | [`../workflow/research.md`](../workflow/research.md) + skill `decision-cycle` | portfolio outcome と長期horizon calibration |
| 資本・cap・sizing | [`../portfolio-management.md`](../portfolio-management.md) + `src/baibai_engine/position/policy.py` | 保有 outcome |
| OP3 の選定判断（深度契約・narrative 規約） | [`../operations/decision-cycle.md`](./decision-cycle.md#opportunity-human-review-gate-op3) | 判断コホート比較（`screening shortlist outcome`）+ 人間ゲートの機会費用・資本deployment（`python tools/measure_deployment_opportunity.py`。cycle結論 × machine top-1 forward return × ledger book-cost/cash） |

計測の母数は 2 系統（doctrine §2）: **(a) 保有 outcome**（少数・深い観測。判断品質の最終的な正）と **(b) 較正リプレイ**（全銘柄 × 長期 horizon。手法較正用に件数を桁で補う）。その中間に **(c) 判断コホート**（1 サイクル約 20 件蓄積する OP3 の selected / rejected）があり、機械順位への付加価値と ploss 判定の序列を記述比較する。(c) は非ランダム割当・窓の重複・少数標本のため因果効果を主張せず、手順変更は長期 horizon の evidence と事前登録を通す。機械レバー（screen / select / E[r]）の実証的改訂は (b) の 3y/5y eligible evidence を必須の関門にし、判断レバー（macro / research 手順）は (a) と運用の事後検証で改める。

## サイクル（1 改善 = 1 issue = 1 PR）

### 0. 現状計測の確認

```bash
uv run baibai-engine screening calibration-build --start 2022-09-01 --end <直近の完全月末>   # 増分。rules 改訂後は --force
uv run baibai-engine screening calibration-evaluate --out .cache/calibration-eval-current.yaml
```

直近の dated report（`reports/` の estimate-calibration 系）と突き合わせ、baseline が固定されていることを確認する。baseline が無い観点を測るときは、まず観察のみの baseline report を書いて固定する。

### 1. 仮説の列挙と issue 化

計測の観察（軸別 IC・decile・トラップ率・replay・保有 outcome のずれ）から改善仮説を挙げ、**issue に登録する**。issue には (i) 観察された事実（レポートへの参照）、(ii) 仮説、(iii) 検証方法（rules variant / 実装変更 / 手順変更）、(iv) 着手条件を書く。

冒頭には[`doctrine.md`の改善提案の価値階層](../doctrine.md#improvement-value-hierarchy)に従い、`価値tier: Tn — <直接的な成果への因果経路>`を1行で書く。T3は観測した頻度・負担、T4を例外採用する場合は人間の実損またはT1〜T3への検証可能な寄与を示す。価値階層を第一基準とし、同じtier内では効果の見込みが大きい順に優先する。

導入後のprimary-research laneのうち完了・review済みをcoverageの分母、screening FV baselineとresearch FVと有効なbridgeがあるものを分子とし、canonical thesisとoperation sessionに保存した非promote laneから同一thesisの再実行、scaffold-only、未review、遡及記入を除いた有効観測が5件以上になったら、乖離率の中央値・範囲、要因件数、`other`率、coverage、ユニーク銘柄数・運用回数を記述集計し、この集計だけでscreening式を変更せず変更仮説は別Issueで事前登録してdesign/confirm検証へ進める。

四半期ごとにapplication DBのshortlist rejected entryとbargain assessment reject / defer laneの`reject_class`頻度を工程別に集計し、頻度上位のうち機械化可能な型をwarning / flag候補として別issueへ事前登録する。分類自体で自動除外やranking変更は行わない。

### 2. 採否基準の事前登録（計測より先に commit）

採用judgeになる数値基準（例: IC・replay 上位の超過リターン差・トラップ非悪化）を、**計測を実行する前に** report の冒頭節または issue に書いて commit する。git history が事前登録の正本。既知の結果（公開済みレポート）がある場合は、盲検性の限定を正直に書く。

### 3. design/confirm 検証

- cohort を時間で 2 分割（design / confirm）し、**両方で同方向・基準充足のときだけ採用**。片側のみは「不確定」、両側逆は「棄却」。
- **rules variant の計測**: 本番 rules を変えずに `SCREENING_RULES_PATH` で variant rules を指し、`--calibration-dir` 相当の別 store（`data/screening/calibration-<variant>/`）へ panel を構築する。rules_hash provenance が本番 panel との混線を機械検出する。
- 有意性は主張しない（cohort 窓は重複し独立でない）。効果量・cohort 勝率・トラップ率で判定する。grid search（基準を後から動かす網羅探索）をしない。

### 4. 採用実装

- 通過した変更だけを本番（`method/screening-rules/` / src）へ反映する。**計測した構成と本番構成を一致させる**（計測に無いレイヤーが本番だけに残ると、検証済み順位が運用で崩れる）。
- rules 改訂後は panel を `--force` 再構築し、本番形（recommended replay）で前後比較を確認する。

### 5. 運用テスト

現 asof でパイプラインを回し、実出力で妥当性を確認する（[`./decision-cycle.md`](./decision-cycle.md) の`opportunity` pathと同じ操作）:

```bash
uv run baibai-engine screening run --asof <最新の完全営業日>
uv run baibai-engine screening select --asof <同上> --run-revision-id <run revision ID>
```

- 変更前後の select 上位の差分を確認し、意図した挙動（例: E[r] 降順の成立・value-trap 形の脱落）を実銘柄で確認する。
- 上位候補が実在の投資判断に耐えるか（明らかな異常・corporate action 起因の歪みがないか）を spot check する。

### 6. dated report で計測記録を固定

`reports/YYYY-MM-DD-<slug>.md` に、再現手順（コマンド）・データ窓・coverage / survivorship の開示・判定表・検算（AP-02）・**採用後の監視事項**を書く。これが一次計測記録であり、別途の監査ファイルは作らない。

### 7. PR → レビュー → マージ → 継続監視

- PR には issue 参照・変更要約・検証結果（design/confirm 表）・運用テスト結果を書く。レビューは [`../anti-patterns.md`](../anti-patterns.md) チェックリスト + 敵対的 self-review（データ⇄結論の整合、比率の再計算、単点で方向を断じない）。
- マージ後、report に書いた監視事項を次のreplay計測で追う。監視で劣化が持続したら新しい issue として次のサイクルに入れる。日常の`opportunity`や`monthly-contribution`をreplay実行の前提にしない。

## 誠実性の規律（doctrine 柱 5 の運用形）

1. **有意性・統計的優位を主張しない**。効果量と cohort 勝率で判断し、そう書く。
2. **仮説と採否基準は検証前に事前登録**し、時間分割の両方で整合した変更だけ採用する。基準を後から動かさない。
3. **survivorship・coverage の欠け・レジーム文脈を計数で開示する**（計測窓がバリュー優位期なら、その旨を結論に併記する）。
4. **累積リターン・年率・シャープ等を実績（track record）として掲げない**。
5. **post-hoc の判断はそう明記する**（例: cap パラメータ選択で confirm を見た場合、その観点の out-of-sample 性は消費済みと書く）。

## Issue / PR 規約

- 1 改善 = 1 issue = 1 PR。レビュー反映・運用テストで見つけたバグ修正・付随する follow-up は同一 PR にコミットを積む。
- issue title は `task(<subsystem>): <改善の要約>` または `improve: <要約>`。本文に観察 → 仮説 → 検証方法 → 着手条件。
- 投資判断の正本は application DB、計測記録は `reports/` に置き、issue / PR には参照と要約を書く（[`./task-runbook.md`](./task-runbook.md) と同じ原則）。
- マージ前ゲート: `ruff format --check` / `ruff check` / `mypy` / `pytest` + write-time negative test + 運用テスト（§5）。
