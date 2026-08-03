---
name: shortlist
description: 買い機会の発見と絞り込み。screening run → select → OP3 narrative 付き shortlist publish → 人間の primary-research set 選択待ちまでを進める。選択後の深掘りは research skill。
---

# Shortlist

全上場銘柄から比較可能な候補群を作り、`/stocks/shortlist` レビュー面で人間へ提示する。終端は人間の primary-research set 選択（推奨 2〜4 件、上限は selection の `research_selection_target_max`）。AI は broker 操作へ進まない。

## 前提

1. AGENTS.md の session 規約に従い `operation start --kind opportunity --as-of <最新完全営業日>`（active があれば resume）。
2. `uv run baibai-engine position ledger --db data/app/baibai.sqlite` で holding / reservation / cash を読む。
3. macro context head の鮮度を判断する: `baibai-engine macro context head` → head が古い、または[深度契約](../../../docs/reference/macro.md#depth-contract)を満たさないと判断したら、先に `macro-context` skill で書き直す。
4. 候補比較の優先順位は [doctrine](../../../docs/doctrine.md)（永久損失 → 5 年期待値と FV 乖離 → portfolio 追加価値 → 購入可能性）。cash・集中・保有はannotationであり、上位候補の hard 除外に使わない。

## 手順

1. **データ準備**（日次 batch が当日分を完走済みなら 4 へ）:

   ```bash
   uv run baibai-engine screening verify-cache-coverage --asof <ASOF>
   # 不足 source があるときだけ: uv run baibai-engine screening bootstrap-cache --asof <ASOF>
   uv run baibai-engine screening extract-edinet-metrics   # coverage 完了でも省略しない
   ```

   coverage が future-dated / stale JPX なら停止（historical backfill 以外で `--allow-stale-jpx` を使わない）。
2. `uv run baibai-engine screening run --asof <ASOF>` → `run_revision_id` を保持。
3. `uv run baibai-engine screening select --asof <ASOF> --run-revision-id <ID> --longlist-top 20 --output-path <workdir>/selection.yaml` → `selection_id` を保持。longlist 20 件は点検 view であり全件深掘りの命令ではない。
4. **差分確認**: 前回 shortlist（application DB）と ticker 集合を new / continued / exited で比較する。continued も narrative を自動継承せず、順位差・価格・最新開示・countercase を再確認する。前回を確認できない run は全候補を確認する。
5. **開示スキャン**: selected 候補（full review では全候補）の直近開示をタイトルレベルで確認し、as-of 財務に無い material 開示（業績修正・資本政策・TOB 等）を narrative の `why` / `counter` へ反映する。
6. **annotation 消化**（不変条件: 判断面へ annotation を足す変更は、この表へ消化規則を同時に足す）:

   | annotation | 消化規則 |
   | --- | --- |
   | FV convergence warning（`price_at_or_above_all_fv_anchors`） | selected / rejected を問わず明示消化する。黙殺しない |
   | `margin_short_to_adv` / `margin_week_end` | 需給の確認材料。単独で自動除外・rank 変更に使わない |
   | E[r] 履歴帯（較正 quintile 文脈） | 帯の記述統計としてのみ参照。個別銘柄の予測として書かない |
   | `data_quality_flags` / `durability_warnings` | flag が upside / downside をどちら向きに歪めるかを narrative に書く |
   | `stale_fin_flag` / `fin_latest_disclosed_date` | 発表済みだが機械行が未反映。**一次開示を先に読み**、narrative の数値をそちらへ寄せる。flag が立つ銘柄を反映前の数字のまま selected にしない |
   | `next_earnings_status`（scheduled / announced / estimated / unknown） | `announced` は as-of 当日までに発表済み、`estimated` はカレンダー欠落時の推定日。推定を確定日として event risk 判定に使わない |

7. **OP3 深度契約**: selected 各銘柄について次を 1 項目ずつ機械的に突合する（印象で「満たしているはず」としない）。

   1. `upside` から希望的前提を剥がしても現行事業の正常化だけで期待値が正か
   2. 利益がピーク外挿でなく複数期レンジの正常利益か（循環のどこにいるかを書く）
   3. `catalyst` が日付または特定可能な event か（dated なら `catalyst_date`。undated なら再評価の観測条件を書く）
   4. 深掘り〜保有初期の dated event（決算・guidance・規制・macro monitoring）を消化したか
   5. リスク調整後に現金保有へ勝るか。net cash / 簿価を床にする銘柄は還元機構を確認したか
   6. carry 支配型で、予想 DPS の前期比跳ね・`forecast_special_gain_flag`・FCF の配当カバー・buyback の一回性（単発 ToSTNeT / 完了済み TOB・program は繰り返さない）を剥がしても成立するか
   7. `rank` が機械 E[r] 降順から乖離する銘柄は理由を書いたか
   8. macro connection の research hint / sizing caution / estimate_caveats / bargain_topography のうち該当分を消化したか（該当なしの判断も書く）
   9. rejected 全件に具体的理由と `reject_class`（disposition_reason が正本、class は集計専用）

8. **publish**: [`tools/shortlist/draft-template.yaml`](../../../tools/shortlist/draft-template.yaml) を写して記入し、source `selection_id` へ束縛して `uv run baibai-engine screening shortlist publish <draft>`。件数契約は 8〜10 件だが、基準を下げて枠を埋めない（selected 0 件も正常で、その cycle は shortlist が正本判断になり session をここで complete する）。draft に `er_annual` を書かない（publisher が bound run から焼き込む）。
9. **検証**: publish された全 entry の焼き込み E[r] を bound run と機械照合する。stderr に印字される follow-up task 提案（rejected の決算日 re-entry trigger）から `task add` を実行する。
10. **cloud 反映**: `tools/cloud/r2_transfer.sh push-app` → `gh workflow run cloud-materialize` → run の completed success を確認。
11. **checkpoint と報告**: session checkpoint を更新し、レビュー面（`/stocks/shortlist`）へ誘導する報告を出す。各 ticker に TradingView link（`https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A<code>`）を付け、件数契約からの逸脱・機械順位との乖離・残 risk を明記する。人間の選択を待つ（session は active のまま `research` skill へ）。

## 既知の gotcha

- run store は 3 世代 retention。selection output のローカルファイルを消しても `screening selection show --selection-id <ID>` で読み直せる（bound run の evict 後も取れる）。
- `select` の再実行は**新しい selection を publish する**（冪等でない）。既存 selection の再取得には使わない。
- machine recommendation を shortlist と呼ばない。review 済み draft の publish だけが shortlist である。

## 参照

- 深度契約の背景・screening 判断境界: [`docs/reference/screening-runtime.md`](../../../docs/reference/screening-runtime.md)
- 資金・注文額 baseline: [`docs/portfolio-management.md`](../../../docs/portfolio-management.md)
