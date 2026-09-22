---
name: research
description: 人間が選んだ企業を一次情報で調査し、企業評価と独立Review、資本配分判断へ進める。
---

# Research

## 開始条件

canonical Triageのresearch候補から、人間が選んだResearch Setを扱う。選定を明示的に委任された場合は、その範囲で選び、理由を示す。選択集合の外のtickerを追加しない。空のResearch Setは正常な見送りであり、Operationを作らない。

`prepare`はexact Triageと選択集合に束縛したcapital-allocation Operationを開始する。同じTriage・同じ集合のOperationがactiveなら再利用し、その同じOperationを継続する。異なるbindingの別Operationがactiveなら新しいResearchは開始しない。この場合は既存Operationを第5節の条件に従って完了する。

## 1. Workspaceを準備する

```bash
uv run baibai-engine research prepare \
  --research-triage-id <RESEARCH_TRIAGE_ID> \
  --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> \
  --ticker <SELECTED_TICKER>
```

複数銘柄は同じ呼出しで`--ticker`を選択数だけ指定する。開始後の対象はpublished TriageとOperation bindingに従い、別日のlatestやrun storeの候補へ差し替えない。執筆済みworkspaceを切り替える場合は元directoryを保持し、新しいworkspaceでbindingとReviewを確認する。

<a id="company-research"></a>

## 2. Caseごとの企業評価を確定する

保存済み年次のセグメント・負債満期は[固定releaseのResearch query](../../../docs/reference/market-lake.md#edinet-research-query)から参照できる。欠測・書類状態未確認の場合は原典確認へ戻る。

この節は企業の調査・算術・独立Reviewを扱う。Position Reviewから利用する場合は、新規Researchのprepare・Operation・CAA・Planningへ進まず、保有用workspaceを使う。

1. `research thesis-scaffold`でdraftを作り、明示mappingされたplaybookの問いを一次資料で検討する。[事業モデル別調査](../../../docs/reference/business-model-research.md)は指定された試行の補助として使い、全社共通の追加gateにしない。
2. [Thesis reference](../../../docs/reference/thesis.md)に従って企業評価とBase/Downsideを構成する。束縛されたMacro Contextのestimate caveatは、materialなものだけを仮定・source・反対仮説へ接続する。機械E[r]とnormalized PERを独立した企業評価へ転記して済ませない。
3. `research evaluate`で計算資料とerrorsを確認する。必要価値・時間感度の解釈は[valuation context](../../../docs/reference/thesis.md#valuation-context)に従う。Review未添付だけの`review_required`は計算資料があってもexit 2であり、他のerrorを無視する許可ではない。
4. `research review-scaffold`を用い、作者と別の作業者がsource・算術・経済的反証を確認する。`evaluate --review`で一組を検証し、coreを変えたらReviewを取り直す。
5. `candidate / defer / reject`を確定し、`research promote`でThesisとReviewを公開する。重要な根拠不足はdefer/rejectとして完成できる。将来見積りに幅があることだけを不合格にせず、資料で確認すべき事実の欠落とは区別する。

## 3. 配分を比較して公開する

全caseの公開状態を`research status`で確認し、CAAのdraftを作る。企業別Reviewへ候補間の比較を複写しない。

```bash
uv run baibai-engine research capital-allocation-scaffold \
  --db stores/application/baibai.sqlite \
  --capital-allocation-assessment-id <ASSESSMENT_ID> \
  --asof YYYY-MM-DD \
  --research-triage-id <RESEARCH_TRIAGE_ID> \
  --thesis-id <THESIS_ID> --out <DRAFT>
```

`--thesis-id`を調査した全case分指定する。比較の内容は[CAA reference](../../../docs/reference/capital-allocation-assessment.md)に従い、重要仮定・反証と採用/見送り理由をAssessmentだけに書く。

```bash
uv run baibai-engine research capital-allocation-publish \
  <DRAFT> --db stores/application/baibai.sqlite --check
```

確認したdigestに対して作者とは別の役がcontent reviewを行う。修正した場合は再reviewし、一致した原稿を公開する。

```bash
uv run baibai-engine research capital-allocation-publish \
  <REVIEWED_DRAFT> --db stores/application/baibai.sqlite
```

## 4. 当日の注文案と人間判断

公開済みCAAのallocate対象だけに`research plan-limit --capital-allocation-assessment-id <ASSESSMENT_ID>`を使う。対象の企業評価はcanonicalなThesis/Reviewから解決する。正式評価日と最新確定quoteを確認し、古い企業評価を更新する場合は元資料を引き継いで変更部分を再Reviewする。

発注sessionは既存calendarで確認できる当日または次の有効営業日を`--target-session`へ指定する。返された有効期限をbroker報告へ渡し、失効・calendar不明の案はdeferする。価格超過は注文案のdeferであり、企業rejectや保有exitへ変換しない。

no_allocation/deferでは発注へ進まない。allocateでも人間のapproveとbroker操作を待ち、実際に報告された事実だけを[Ledger Record](../ledger-record/SKILL.md)へ渡す。既保有・予約等の追加購入条件、cash不足とwarningの違いは[資本方針](../../../docs/portfolio-management.md)とCLI結果に従う。

同じResearch Setから順次配分する場合は、CAA-1の人間報告をledgerへ反映してから、最新cashでCAA-2を判断する。企業調査を繰り返す必要はない。

## 5. Operationを完了する

`operation checkpoint`または`operation complete --payload <FILE>`へ、開始時のresearch_triage artifact・exact ref・Research Setを保ったpayloadを渡す。完了時は最後に公開したCAAを`capital_allocation_assessment` artifactとして参照する。Thesisだけのpublished状態をOperation完了としない。

公開済みCAAが`no_allocation`なら、追加の確認を求めず、配分なしの結果と次のtriggerを記録してOperationを完了する。`human_confirmation`は不要。将来の追跡taskが残っていても今回の完了を妨げない。`allocate / defer`では人間の裁定を記録して完了する。

必要なmonitoringは既存taskを確認して登録する。cloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。対象未確定、重要な入力矛盾、独立Review未完了、binding競合は解消するまで先へ進まない。
