---
name: research-triage
description: 保存済みReview SetをTriage判断し、人間のResearch Set選択へ渡す。
---

# Research Triage

## 手順

1. machine storeを同期する。失敗したら分析へ進まない。

   ```bash
   batch/scripts/pull.sh
   ```

2. 対象日を確定してrunnerを実行する。既定は当日JSTであり、非営業日でも前営業日へ自動で戻らない。「最新」の依頼では同期した[保存済みReview Set](../../../tools/owner_mcp/README.md#triage前のreview-set)の`as_of / run_at`と、market calendar・日次batchの完了状況を照合する。最新の完了営業日を使う場合は、その日を`--asof YYYY-MM-DD`へ明示する。取得失敗や対象日のReview Set未作成を「候補なし」と扱わない。

   Screening Run・Review Setの生成が必要なら、先に[日次machine処理](../../../batch/OPERATIONS.md#日次機械工程--baibai-batch-daily)を完了する。このrunner自体は新しい市場データを取得せず、Review Setも生成しない。

   ```bash
   uv run baibai-batch analysis run
   ```

3. [終了状態](../../../docs/reference/analysis-operations.md#終了状態)を確認する。新しい発行が成功した`published_awaiting_human / published_all_skip`ではapplication DBをクラウドへ反映する。

   ```bash
   batch/scripts/publish.sh
   ```

   正常なno-opだけで新たなpublishを実行しない。ただし前回のクラウド反映が未完了なら、Triageを再生成せず[application反映手順](../../../batch/OPERATIONS.md#application-db-を反映する)で完了させる。

4. research候補がある場合は、返されたexact Triage ID・as-ofを示す。選定を明示的に委任された場合は指定範囲の候補と選定理由を示して進む。それ以外はpriority順の全候補、理由、調査質問、主要リスクを示し、人間の選択を待つ。全skip・対象なしでは選択を求めない。

5. 選択したtickerとexact Triage IDを[Research](../research/SKILL.md)へ渡す。Triage発行ではOperationを開始しない。

## 失敗時

stdoutだけで原因が分からない場合に表示されたprivate logを確認する。入力・adapter・binding等の原因を解消し、同じ対象日でfreshに実行する。runnerのexact照合に任せ、blind retryや過去の成功結果で補完しない。

## 判断規則

AI判断の正本は[TRIAGE_POLICY](../../../batch/src/baibai_batch/analysis/policy.py)であり、このskillの本文を重ねてmodel inputへ渡さない。入力・保存・公開の境界は[runner reference](../../../docs/reference/analysis-operations.md)に従う。
