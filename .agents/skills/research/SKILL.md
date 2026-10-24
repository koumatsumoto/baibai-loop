---
name: research
description: 人間の明示開始から有限対象を選び、企業評価・独立Review・新規または保有の提案を完成する。
---

# Research

## 開始と固定対象

通常の「投資提案して」は`discover_new`、候補外も含む銘柄指定は`specified_stocks`、保有見直しは`review_positions`。同じ依頼で選定から具体提案まで進め、全候補Triageや人間による事前銘柄選択は要求しない。newとholdingを混ぜず、追加買付依頼は[現行方針](../../../docs/doctrine.md#investment-strategy)と照合する。

```bash
uv run baibai-engine research selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db stores/application/baibai.sqlite --runs-path stores/screening/runs.sqlite
```

指定は`--scope specified_stocks --ticker <TICKER>`、保有は`--scope review_positions`を使う。ユーザーの目的から価値獲得の仮説を立て、五領域・保有・既存調査を比較する。必要な候補外の対照も同じ固定runから有限に読む。機械順位の極端値だけを順に選ばず、capacityを弱い対象で埋めない。

企業成長L2の原系列はJ-Quants財務サマリーを使う。L3でEBITDAの構成・減価償却等を詳しく調べる場合はEDINET原典へ戻り、期間・科目・連結基準を照合する。EDINET詳細抽出を成長L2の実行条件にはしない。企業成長は配置済みの固定release mirrorを読む。別配置ならprepareの `--company-growth-mirror` で指定し、mirror不足を作業SQLiteの更新行で代替しない。企業成長の会計期末範囲は、問いに応じてdraftの `company_growth_start` / `company_growth_end` へ指定できる。[期間・欠測と入力固定](../../../docs/reference/company-growth.md)を確認し、PER/peerの未提供やcoverage未確認を企業価値のInputGapへ加えない。

contextのrun・Research日・cutoff・requestをdraftへ引き継ぎ、対象と問い、選定理由、比較した代替と未確認の範囲を記す。runを別途読む時はcontextとrevision・analysis日が一致し、run_atがcutoff以前であることを確認する。全runをLLMへ一括投入しない。

機械入力の不足は経済的棄却と分ける。既存のticker-profile・coverage確認と必要な通常取得で補い、得られない値はInputGapへ残す。単一対象の不足を全期間backfillで埋めない。新たな共通市場判断を今回作る場合は[市場判断skill](../market-context-assessment/SKILL.md)を先に完了する。

```bash
uv run baibai-engine research prepare \
  --selection-file <SELECTION_JSON> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --sqlite-path stores/market/market.sqlite
```

Operationが対象・問い・唯一の入力snapshotを保持し、`domain-inputs.yaml`はその読取投影となる。新しい資料は評価のsourcesへ加え、開始入力へ挿入しない。固定は全入力のintraday PIT再現を保証しない。観測日、公表/受領cutoff、Readingのread_semantics、正式評価日を区別する。

企業成長の通常投影は保存済み結果の直近12会計期をFY/累計の薄い表と既存YoYで示す。保存結果とdraftの要求範囲は削らない。長期の問いにはprepareの `--company-growth-periods 24` または `all` で表示だけを広げる。既存workspaceの切替は `--force` を付け、`full_result_ref` の完全結果は同じOperationの保存Selectionから読む。四半期単独・TTM・BS/CFは完全結果に残り、FY/累計を単独四半期と読まない。

## 評価と独立Review

1. `research assessment-scaffold --workspace <WORKSPACE> --ticker <TICKER> --asof <YYYY-MM-DD>`で作る。返された`assessment_draft`を編集し、以下の`<DRAFT>`にそのpathを使う。保有は対象のholding子workspaceを使う。[Stock Assessment](../../../docs/reference/stock-assessment.md)から判断を変える経済的な問いを選び、一次資料へ戻る。
2. 事業・資本・分配・期間からmodelと要求率を選び、Base/Downsideと最強反証を組み立てる。数値計算には`research calculate <INPUTS>`を使える。PDFの抽出が空・崩れた場合は原ページへ戻り、記載なしやゼロとしない。
3. `research assessment-evaluate <DRAFT>`で確認する。`research assessment-review-scaffold --workspace <WORKSPACE> --ticker <TICKER>`からReviewを準備し、作者と別の作業者が原典・仮定・算術・反証を検討する。作者の入力や出力のコピーを独立検算としない。
4. `research assessment-evaluate <DRAFT> --review <REVIEW>`で一組を確認し、`research assessment-promote --workspace <WORKSPACE> --ticker <TICKER>`で保存する。core変更時はReviewを取り直す。数値未確定でも所見と理由を独立検証した評価は完成でき、調査自体の不足は未完了とする。

別日に当日算術を使う場合は`assessment-scaffold --from-assessment-id <ID>`で差分draftを作る。根拠の元日付を保ち、残る期間・今後分配・株式単位と今回の理由を確認して新しいReviewを取る。旧horizonの自動延長や受取済み分配の持越しはしない。

## 全対象の提案

statusとOperationを読み、保存評価は`operation record-assessment <OPERATION_ID> --assessment-id <ID>`で束縛する。未完了は理由と参照を記録し、対象を自動補充しない。

```bash
uv run baibai-engine operation --db stores/application/baibai.sqlite record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <SOURCE_OR_WORKSPACE_REF>
uv run baibai-engine research proposal-context \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --sqlite-path stores/market/market.sqlite
uv run baibai-engine research proposal-scaffold \
  --operation-id <OPERATION_ID> --proposal-id investment-proposal-<UUID> --out <DRAFT>
```

[Investment Proposal](../../../docs/reference/investment-proposal.md)に従い全固定対象を被覆する。完成した比較によるno_actionと、調査・条件不足のdeferを分ける。実quoteと試算limit、保存条件と現在previewを区別する。0対象では空Proposalを作らず、選定理由を保って終了する。

```bash
uv run baibai-engine research proposal-check <DRAFT> --db stores/application/baibai.sqlite
uv run baibai-engine research proposal-publish <REVIEWED_DRAFT> --db stores/application/baibai.sqlite
```

checkのvalidとreview_bindingを別に読み、作者と別の作業者が正規modelのdraft_sha256へ内容Reviewを束縛する。staleやfile hashで代用しない。入力事実が変わればsnapshotとReviewを取り直す。公開後の`proposal-preview --proposal-id <ID>`は現在再試算で、保存提案・取引事実を更新しない。

## 完了と再開

公開済み提案を`operation record-proposal <OPERATION_ID> --proposal-id <ID>`へ記録し、現在payloadの固定selection/outcomesを保ってresult/nextを記し、`operation complete <OPERATION_ID> --payload <FILE>`へ渡す。buyを含む完了には具体termsが必要。sellのterms不足は理由付きで調査工程を終了できるが、価格・数量まで完成したと報告しない。人間の採否・broker操作・実約定を完了条件へ補完しない。

```bash
uv run baibai-engine research prepare \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --force
```

再開は保存入力から投影を復元し、元runやmutable storeへ付け替えない。forceは生成投影を修復し、執筆済みdraftを保つ。commit後のfile失敗も返されたOperation IDから再開する。別activeを上書きせず、完了済みへ追記しない。同文の本当の再依頼は新workspaceと新Operationで扱う。

後続調査は現在の成果を確定して次Operationへ進む。実際に報告された注文・資金は[Ledger Record](../ledger-record/SKILL.md)、本番反映は[application反映](../../../batch/OPERATIONS.md#application-db-を反映する)へ進む。monitoringは同じ問いの既存taskを確認して記録する。
