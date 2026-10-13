---
name: research
description: ユーザーの明示開始で有限対象を調査し、独立Reviewを経て新規・保有提案を完成する。
---

# Research

## 開始条件

ユーザーが明示的に調査・評価・提案を開始したときだけ実行する。保存済み三領域、保有、既存調査を読み、新規・保有を合わせて最大3銘柄、0件も正常として選び、選定理由を示す。新規はexact Triageのresearch admission内、保有は現在ledgerの対象から選ぶ。上限を埋めるための弱い対象追加、脱落ごとの補充、日次自動起動は行わない。

有限集合と開始要求はOperationが所有する。厳密な被覆・完了・再開・既存activeの扱いは[Research進行](../../../docs/reference/analysis-operations.md#明示開始と固定対象)が正本である。選定理由や開始記録を人間の最終採否と表現しない。

## 1. Workspaceを準備する

```bash
uv run baibai-engine research prepare \
  --request '<USER_START_REQUEST>' \
  --research-triage-id <RESEARCH_TRIAGE_ID> \
  --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> \
  --ticker <NEW_TICKER> \
  --holding-ticker <HELD_TICKER> \
  --sqlite-path stores/market/market.sqlite
```

対象purposeごとに必要なflagだけを使う。新規は`--ticker`、保有は`--holding-ticker`を同じ呼出しで指定し、合算上限を守る。新規のない開始はTriage IDを省略して`--asof`を指定できる。0件も開始要求と提案なしの終了を残す。

新規workspaceと`holdings/<ticker>`は同じOperationを参照する。AIの読取入口は親の`domain-inputs.yaml`一本とし、選択済み新規の機械snapshot・保存Triageの調査理由/質問/リスクと、選択済み新規/保有の三領域入力を読む。観測・機械anchor・保存判断・今回置く仮定を分ける。`research-workspace.yaml`の全候補snapshotは内部のcanonical照合用であり、AIへ再度全文を渡さない。保有子workspaceのmanifestにある`domain_inputs_ref`は子workspaceを基準に親入力を指す。同じ作業では共通入力を重ねて読まず、保有単独の開始は自身の`domain-inputs.yaml`を読む。保存された旧章立てreportの全文作成を前提にせず、materialな環境変化だけ企業評価と提案へ接続する。既存Thesisがあれば`--from-thesis-id`で引き継ぐ。再開で別Triage・対象集合へ差し替えず、執筆済みworkspaceを保持する。

<a id="company-research"></a>

## 2. Caseごとの企業評価を確定する

保存済み年次のセグメント・負債満期は[固定releaseのResearch query](../../../docs/reference/market-lake.md#edinet-research-query)から参照できる。欠測・書類状態未確認の場合は原典確認へ戻る。

選択済み入力の`cash_flow`と3FY平均EPSベースの機械倍率は、[Valuationの意味とCF文脈](../../../docs/reference/valuation-metrics.md#researchへ渡すcf文脈)に従って読む。

市場・業種との個別相対値は[Market Context読取](../../../docs/reference/market-context.md#market-context-reads)で比較基準日・母集団とともに確認し、乖離を企業価値認定へ置き換えない。

外部の目標値・rating・scoreは[External Assessmentの保存済み照会](../../../docs/reference/market-lake.md#external-assessment-query)で出所・尺度・時点とともに参照し、同原評価元を独立票として数えず、企業固有評価の代用にしない。

この節は企業の調査・算術・独立Reviewを扱う。保有を扱う場合は同じ有限Operationの保有workspaceを使い、新規CAA・購入Planningへ混ぜない。

1. `research thesis-scaffold`でdraftを作り、明示mappingされたplaybookの問いを一次資料で検討する。[事業モデル別調査](../../../docs/reference/business-model-research.md)は指定された試行の補助として使い、全社共通の追加gateにしない。
2. [投資目的とリスク許容](../../../docs/doctrine.md#investment-strategy)を判断の前提とし、[Thesis reference](../../../docs/reference/thesis.md)に従って企業評価とBase/Downsideを構成する。束縛された市場環境判断の含意・留意点は、materialなものだけを仮定・source・反対仮説へ接続する。機械推計の照会・意味は[Valuation metrics](../../../docs/reference/valuation-metrics.md#machine-valuation)を参照し、企業固有評価との区別はThesis referenceに従う。
   利益の1株換算には直近の自己株控除株数を起点とし、その資料の重要後発事象と評価日までの一次開示で増資・自己株処分・消却を照合する。株式分割の確認だけで株数の更新を済ませず、実行済みと予定を分けて計算basisに残す。
   ratio-v1とProjectionの数値は、[draft算術](../../../docs/reference/thesis.md#draft-calculation)の`research calculate <INPUTS>`で生成し、必要な既存欄へ渡す。terminal/cashの元となる企業見積りと算式は一次資料から別途確認する。
3. `research evaluate`で計算資料とerrorsを確認する。必要価値・時間感度の解釈は[valuation context](../../../docs/reference/thesis.md#valuation-context)に従う。Review未添付だけの`review_required`は計算資料があってもexit 2であり、他のerrorを無視する許可ではない。
4. `research review-scaffold`を用い、作者と別の作業者がsource・算術・経済的反証を確認する。`evaluate --review`で一組を検証し、coreを変えたらReviewを取り直す。
   更新時は、過去の不足が評価日までの新しい一次資料で解消していないか確認する。不明事項は重要な事実欠落と見積りの幅を分け、判断を左右するものは結論が逆転する条件と程度を既存欄に記す。利益・倍率・要求見返りで同じリスクの影響を重複控除せず、利益悪化と資金制約による倍率低下など経済経路が異なる影響は区別して反映する。根拠付きの回復・成長をBaseから一律に除かない。
   Review側も資料・単位・期間・重要仮定を独立に確認した入力で`research calculate <REVIEW_INPUTS>`を呼び、`recalculated_projections`を既存Review欄へ渡す。作者の出力や入力をそのままコピーして独立検算としない。計算成功は公開可否や事業前提の確認を意味しない。
5. `candidate / defer / reject`を確定し、`research promote`でThesisとReviewを公開する。経済的に判断できたdefer/rejectは完成できる。調査自体が終わらない根拠不足は未完了として記録し、経済的見送りに偽装しない。将来見積りに幅があることだけを不合格にせず、資料で確認すべき事実の欠落とは区別する。

## 3. 配分を比較して公開する

`research status`と`operation show`で成果を確認する。新規のpromoteはReviewed Thesis参照をOperationへ記録する。適用可能な既存評価を使う場合は`operation record-thesis <OPERATION_ID> --thesis-id <THESIS_ID>`で明示して束縛する。

調査が終わらない対象は次で理由・既存参照を残し、代替対象を補充しない。

```bash
uv run baibai-engine operation --db stores/application/baibai.sqlite record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <SOURCE_OR_WORKSPACE_REF>
```

完了した新規だけでCAAのdraftを作る。全未完了・0件ならCAAを作らず第5節へ進む。企業別Reviewへ候補間の比較を複写しない。

```bash
uv run baibai-engine research capital-allocation-scaffold \
  --db stores/application/baibai.sqlite \
  --capital-allocation-assessment-id <ASSESSMENT_ID> \
  --asof YYYY-MM-DD \
  --research-triage-id <RESEARCH_TRIAGE_ID> \
  --thesis-id <THESIS_ID> --out <DRAFT>
```

`--thesis-id`はOperationに記録した完了新規の全件だけを指定する。CAA scopeには未完了の理由・参照も固定される。保有は[Position Review](../position-review/SKILL.md)の検証済みdraftを別成果として残す。比較の内容は[CAA reference](../../../docs/reference/capital-allocation-assessment.md)に従い、重要仮定・反証と採用/見送り理由をAssessmentだけに書く。

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

後続配分は先行注文の人間報告をledgerへ反映し、新しい明示開始で最新cashを再確認する。適用可能な既存Reviewed Thesisを再利用でき、無条件に企業調査を繰り返さない。

## 5. Operationを完了する

`operation show`の現在payloadを基に、固定selectionと成果を保持して`result / next`を記入し、`operation complete --payload <FILE>`へ渡す。新規完了があればその範囲の公開済みCAAを`capital_allocation_assessment` artifact/refで参照する。保有は検証済みdraftがOperationに残っていることを確認する。

これはL3提案完成・調査終了の記録であり、いずれのCAA結果でも人間の採否を完了条件にしない。人間未採否を`human_confirmation`へ補完せず、採否・broker操作・実約定の報告は別工程とする。未完了を含む選択集合の被覆を満たしたら、全未完了や0件も理由を残して終了できる。

必要なmonitoringは既存taskを確認して登録する。cloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。対象未確定、重要な入力矛盾、独立Review未完了、binding競合は解消するまで先へ進まない。
