---
name: macro-context
description: 人間の明示的な依頼でfull-depthのMacro Contextを評価し、独立review後に発行する。
---

# Macro Context

人間の明示的な依頼で実行する。日次の機械series/Reading更新からContext執筆を自動起動しない。成果物の構成と深度は[macro reference](../../../docs/reference/macro.md#depth-contract)に従う。

## 外部Chatとの引継ぎ

外部draftは[Macro handoff](../../../docs/reference/macro-handoff.md)に従って原稿と入力を照合する。構造検証済みのhandoffも未発行の入力であり、以下のreviewを省略しない。

## 手順

1. **今回の入力を固定する。** 対象as-ofのseries/Reading・market snapshotを用意し、同期はOps Maintenanceに従う。application DBをpullしない。Readingの鮮度・不足・極値を確認し、任意の一系列の不足だけで全工程を止めない。indicator inputは次のspec形式から生成する。

   ```yaml
   asof: 2026-07-27
   sections:
     金利・金融政策:
       - us.10y
     為替:
       - usd_jpy
   ```

   asofと系列は今回の対象に置換する。asofは引用符なしのISO日付。accessed_at省略時は実行時刻になる。specを`/tmp/spec.yaml`へ保存して実行する。

   ```bash
   uv run python -m baibai_engine.macro.context.scaffold_inputs /tmp/spec.yaml --output /tmp/inputs.yaml
   ```

   出力はdraft全体ではなく`inputs.indicator_series`へ組み込む配列である。他のinputsを消さず、採用観測・版・実効窓を確認する。

2. **前回を読まず今回の評価を作る。** 今回の一次情報からcore/synthesis/scenario/connectionを作る。前回Context本文・確率・scorecard条件・同内容reportには接触しない。事前接触した場合は汚染のないsessionへ執筆を引き渡す。regime_summaryの必須比較fieldは初回checkまで次を使う。

   ```yaml
   change_since_previous: 今回の独立評価を固定中。前回比較は初回check後に実施する。
   previous_scorecard_review: 前回scorecardは初回check後に確認する。
   previous_scorecard_snapshot_id: null
   ```

   ```bash
   uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml --check
   ```

3. **初回check後に前回を比較する。** 実際の前回Contextを確認し、比較対象IDと今回のasofを指定してscorecardを取得する。同じas-ofの別publicationやheadを無条件に前回としない。以下の非秘密ID・日付は直前に確認した実値へ置換し、各commandと同じshell呼出し内で設定する。別の呼出しへ変数が残る前提にしない。

   ```bash
   previous_context_id='確認済みの前回Context IDに置換'
   asof='YYYY-MM-DD'
   uv run baibai-engine macro context scorecard \
     --context-id "$previous_context_id" --asof "$asof" --format json
   ```

   応答の`machine_snapshot`全体を`inputs.machine_snapshots`へ組み込み、その`input_id`をregime_summaryの`previous_scorecard_snapshot_id`へ設定する。仮記載は実際の比較結果へ置換して再checkする。未取得を「前回なし」と書かず、採点不能・観測不足も事実どおり記す。今回の結論を前回に合わせない。

4. **文章編集と独立reviewを行う。** [判断文書の編集](../../../docs/reference/judgment-writing.md)に従い、authorと別sessionのreviewerへ固定draftとsourceを渡す。review中はdraftを変えず、指摘後に修正する。新しいsource・因果・評価を採用したら、影響する判断と下流を再確定する。独立reviewは初回を含め最大3巡、原稿変更で巡数をリセットしない。3巡目もBLOCKEDなら修正・checkまで行い、発行せず人間へ上げる。再開は人間のdraft承認または追加review指示に従う。

5. **最終check・発行・読戻しを行う。** 仮記載を残さず、review PASSまたは前項の人間承認後に最終checkする。checkは文書・registry契約の検査で、store/CASとknown reading revisionの検査は実publishで行う。

   ```bash
   uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml --check && \
     uv run baibai-engine macro context head
   ```

   checkとhead取得の成功後、返ったYAMLの`context_id`値だけを`expected_head`へ設定する。既存headがある場合:

   ```bash
   expected_head='確認済みのhead context_idに置換'
   uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml \
     --expected-head "$expected_head"
   ```

   headがnullの場合だけ引数を省略する。

   ```bash
   uv run baibai-engine macro context publish /tmp/macro-context-draft.yaml
   ```

   CAS競合は理由を確認し、expected-headだけ更新して無条件再試行しない。発行結果のIDを`context_id`、対象日を`asof`に設定し、そのexact revisionを確認する。

   ```bash
   context_id='今回発行したContext IDに置換'
   asof='YYYY-MM-DD'
   uv run baibai-engine macro context show --context-id "$context_id" --asof "$asof"
   ```

   cloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)へ進む。Contextはapplication DBなので、macro観測storeの`push-macro`だけで反映完了にはならない。

## 判断の境界

Macroから売買時期、現金比率、個別sizing、候補のhard gate、機械rankingの変更を直接指示しない。重要な根拠不足、一次sourceと判断の矛盾、必要な独立reviewの未完了があれば発行しない。
