---
name: research-triage
description: 明示依頼で保存済みReview SetをTriage判断し、有限なAI調査選択へ渡す。
---

# Research Triage

## 手順

1. machine storeを同期する。失敗したら分析へ進まない。

   ```bash
   batch/scripts/pull.sh
   ```

2. 「最新」の依頼では、同期したstoreから最新対象を解決してrunnerを実行する。

   ```bash
   uv run baibai-batch analysis run --latest
   ```

   過去日のreplayは`--asof YYYY-MM-DD`で指定する。両optionは同時に指定できない。option省略は当日JSTのexact指定であり、非営業日でも前営業日へ戻らない。取得失敗やReview Set未作成を「候補なし」と扱わない。

   候補選抜と機械評価の照会は区別する。保存済みの候補外銘柄を含むER/FV anchorの意味と読取は[Valuation metrics](../../../docs/reference/valuation-metrics.md#machine-valuation)を参照する。

   Screening Run・Review Setの生成が必要なら、先に[日次machine処理](../../../batch/OPERATIONS.md#日次機械工程--baibai-batch-daily)を完了する。このrunner自体は新しい市場データを取得せず、Review Setも生成しない。

3. [終了状態](../../../docs/reference/analysis-operations.md#終了状態)を確認する。新しい発行が成功した`published_research_available / published_all_skip`では、[実行境界](../../../batch/OPERATIONS.md#actions-write-boundary)と[application反映手順](../../../batch/OPERATIONS.md#application-db-を反映する)に従いクラウドへ反映する。

   正常なno-opだけで新たなpublishを実行しない。前回のクラウド反映が未完了なら、Triageを再生成せず反映手順で完了させる。

4. research候補がある場合はexact Triage ID・as-ofとpriority順の候補、理由、調査質問、主要リスクを示す。Triage発行は調査開始・購入ではない。ユーザーが調査・提案まで明示開始したときは、保有と合わせた有限選択へ進める。上限や現在の目的は[Research](../research/SKILL.md)に従い、枠外の候補を価値のないskipへ書き換えない。

5. 開始要求・選定理由・exact Triage IDと新規/保有purposeを[Research](../research/SKILL.md)へ渡す。日次やTriage発行だけで後続AI調査を起動しない。

## 明示的なmanual draft

通常のrunnerを使わず、人間が指定したReview Setからdraftを作成・発行する場合だけ`research triage scaffold / publish`を使う。型・保存・CASはResearchが所有する[同じ契約](../../../docs/reference/analysis-operations.md#research-triage-contract)に従う。旧ScreeningのTriage writerは使わない。

```bash
uv run baibai-engine research triage scaffold \
  --review-set-id <REVIEW_SET_ID> --runs-db stores/screening/runs.sqlite \
  --db stores/application/baibai.sqlite --output-path <DRAFT>
```

TODOを根拠のある`research / skip`判断へ置換してから発行する。

```bash
uv run baibai-engine research triage publish <DRAFT> \
  --runs-db stores/screening/runs.sqlite --db stores/application/baibai.sqlite
```

発行後のcloud反映と後続Researchの開始条件は前節と同じである。

## 失敗時

stdoutだけで原因が分からない場合に表示されたprivate logを確認する。入力・adapter・binding等の原因を解消し、同じ対象日でfreshに実行する。runnerのexact照合に任せ、blind retryや過去の成功結果で補完しない。

## 判断規則

AI判断の正本は[TRIAGE_POLICY](../../../batch/src/baibai_batch/analysis/policy.py)であり、このskillの本文を重ねてmodel inputへ渡さない。入力・保存・公開の境界は[runner reference](../../../docs/reference/analysis-operations.md)に従う。
