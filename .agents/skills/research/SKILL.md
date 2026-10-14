---
name: research
description: ユーザーの明示開始で有限対象を調査し、独立Reviewを経て新規・保有提案を完成する。
---

# Research

## 開始条件

通常の「投資提案して」は `discover_new` とし、AIが保存分析・候補を比較して対象を選び、同じ依頼で深掘り、独立Review、CAAと具体提案まで進める。全候補Triage発行やユーザーによる銘柄選択を要求しない。銘柄指定は候補外も含む `specified_stocks`、保有見直し・売却提案は売却を前提にしない `review_positions` という同じResearchの別入口とする。保有の深掘りを通常探索へ混ぜない。

既保有への新規追加は現在方針で禁止されている。意図が保有見直しと明確ならそのscopeで開始し、明示的な追加購入依頼には方針を説明する。不明な場合だけ質問する。

有限集合と開始要求はOperationが所有する。[Research進行](../../../docs/reference/analysis-operations.md#明示開始と固定対象)の被覆・完了・再開に従う。深掘り上限は実行policyでありノルマではなく、0件も選定理由を残す。残りの依頼範囲をsummaryで説明し、追加調査は現在の成果を確定して次Operationへ進める。新しい費用・範囲の承認が必要な場合だけ確認する。日次AI自動起動は行わない。

## 1. 選定してWorkspaceを準備する

```bash
uv run baibai-engine research selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db stores/application/baibai.sqlite --runs-path stores/machine/runs.sqlite
```

指定銘柄は `--scope specified_stocks --ticker <TICKER>`、保有は `--scope review_positions` とし、必要なら指定tickerを付ける。contextのrun参照、research_as_of、input_known_atとrequestをdraftへコピーし、比較した選定理由・調査の問い・主要リスク・今回の範囲をAIが記す。構造はCLIのhelpとselection modelが正本である。現在の三領域・保有・既存調査を読み、弱い対象で上限を埋めず、脱落を自動補充しない。

selection-context schema2の`candidates`は同runのraw Stock Analysisで、`candidate_discovery_method`と各行の`candidate.reasons`が機械選抜の方式とApproach内rankを表す。候補0件は完成したrunの正常結果である。候補外の指定・保有対象も同runの全分析から読み、AIの`SelectedTarget.rationale`を機械rankと混ぜない。normalized gapの0とnull、rawの欠落とnullを保持し、候補一覧の別保存・再発行は行わない。

inputs_requiredは経済的見送りではない。既存 `screening ticker-profile`、`verify-cache-coverage`、必要な `bootstrap-cache` / `screening run` / `extract-edinet-facts` と一次資料確認で補完し、同じ依頼でcontextを取り直す。通常の認可済みread取得に再許可を要求しない。新課金・権限変更・本番反映は[Ops Maintenance](../ops-maintenance/SKILL.md)の別境界であり、単一銘柄の不足を無断の全期間backfillで埋めない。標準補完後も機械分析を得られなければInputGapに試したsourceと不足理由を残し、一次資料で企業評価を進める。重要fact自体を得られない対象は未完了として扱う。

```bash
uv run baibai-engine research prepare \
  --selection-file <SELECTION_JSON> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --sqlite-path stores/market/market.sqlite
```

Operation内の一つのsnapshotが固定入力の正本であり、`domain-inputs.yaml`は削除・再生成可能なAI読取投影である。選択済み対象だけの機械値・市場・外部評価と、scope/summary/選定理由/問い/リスクを読む。保有子は `domain_inputs_ref` で親入力を共有する。観測・機械anchor・保存判断・今回の仮定を分け、materialな環境変化だけ企業評価と提案へ接続する。

```bash
uv run baibai-engine research prepare \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --force
```

再開はOperationの保存入力から復元し、元runやmutable storeを再読しない。scope/対象/理由/source/cutoffは固定。`--force`は生成投影を修復し、執筆済みdraftを保持する。DB commit後のfile失敗も返されたOperation IDで再開できる。completedの再送はalready_completedで成果を追記しない。本当に再依頼された同じ文は、新workspaceで別Operationとして開始できる。

分析日が前日でもResearch日は当日。開始入力を当日の分析へ付け替えず、Thesis/Review/CAAは実際の正式評価日で作る。翌日再開でも開始snapshotは不変であり、当日の価格・cash・保有・注文・正式評価条件は再確認する。

<a id="company-research"></a>

## 2. Caseごとの企業評価を確定する

保存済み年次のセグメント・負債満期は[固定releaseのResearch query](../../../docs/reference/market-lake.md#edinet-research-query)から参照できる。欠測・書類状態未確認の場合は原典確認へ戻る。

選択済み入力の`cash_flow`と3FY平均EPSベースの機械倍率は、[Valuationの意味とCF文脈](../../../docs/reference/valuation-metrics.md#researchへ渡すcf文脈)に従って読む。
機械snapshotの実績・予想年間DPSと採用`dividend_basis`が食い違う場合は、対象期、普通・特別／記念配当の内訳、還元方針を原典で確認する。これらは計算用の株式basisへ正規化した値であり、予想の持続性や上限適用理由を値だけから決めない。

小型であることやFCFが0・負であることだけで経済的見送りにしない。サイズ・ADVは執行の制約、負FCFは維持投資/成長投資・運転資金・回収・資金余力の問いへ接続する。CFのsourceと品質・期間を保ち、半期の`approximated`値を通年実績と読み替えず、資料で確認すべき重大な不足を免除しない。

短い価格bar期間も一律の見送り条件にしない。`listing_span_days`は利用可能な価格履歴の日数proxyであり、実上場日や財務履歴ではない。企業評価に必要な財務期間は一次資料で確認し、短い自己価格レンジを長期の価値根拠と読み替えない。

市場・業種との個別相対値は[Market Context読取](../../../docs/reference/market-context.md#market-context-reads)で比較基準日・母集団とともに確認し、乖離を企業価値認定へ置き換えない。

外部の目標値・rating・scoreは[External Assessmentの保存済み照会](../../../docs/reference/market-lake.md#external-assessment-query)で出所・尺度・時点とともに参照し、同原評価元を独立票として数えず、企業固有評価の代用にしない。

この節は企業の調査・算術・独立Reviewを扱う。保有を扱う場合は同じ有限Operationの保有workspaceを使い、新規CAA・購入Planningへ混ぜない。

1. `research thesis-scaffold`でdraftを作り、明示mappingされたplaybookの問いを一次資料で検討する。[事業モデル別調査](../../../docs/reference/business-model-research.md)は指定された試行の補助として使い、全社共通の追加gateにしない。
2. [投資目的とリスク許容](../../../docs/doctrine.md#investment-strategy)を判断の前提とし、[Thesis reference](../../../docs/reference/thesis.md)に従って企業評価とBase/Downsideを構成する。束縛された市場環境判断の含意・留意点は、materialなものだけを仮定・source・反対仮説へ接続する。機械推計の照会・意味は[Valuation metrics](../../../docs/reference/valuation-metrics.md#machine-valuation)を参照し、企業固有評価との区別はThesis referenceに従う。
   新しい通常評価の要求率は[要求年率方針](../../../docs/portfolio-management.md#required-return-policy)を出発点に作者が明記し、個別に高くする独立した理由をcalculationへ記す。scaffoldのnullは未評価として残し、保存済みの率を現在policyで置換しない。
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
  --operation-id <OPERATION_ID> --out <DRAFT>
```

完成新規の全Thesis参照はOperation progressから生成する。CAA scopeには未完了の理由・参照も固定される。保有は[Position Review](../position-review/SKILL.md)の検証済みdraftを別成果として残す。比較の内容は[CAA reference](../../../docs/reference/capital-allocation-assessment.md)に従い、重要仮定・反証と採用/見送り理由をAssessmentだけに書く。

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

夜間の案を翌朝使う場合も、現在の企業評価基準日を確認する。日付が変わったなら`thesis-scaffold --from-thesis-id`で差分draftを作り、価値・期間・未受取cashを確認して新revisionと独立Review、当日のCAAを作る。Operationがactiveなら固定対象のまま続けられる。前夜に完了済みなら新しい明示開始で同じ対象と既存資料を引き継ぎ、完了workspaceへ成果を追記しない。

発注sessionは既存calendarで確認できる当日または次の有効営業日を`--target-session`へ指定する。返された有効期限をbroker報告へ渡し、失効・calendar不明の案はdeferする。価格超過は注文案のdeferであり、企業rejectや保有exitへ変換しない。

no_allocation/deferでは発注へ進まない。allocateでも人間のapproveとbroker操作を待ち、実際に報告された事実だけを[Ledger Record](../ledger-record/SKILL.md)へ渡す。既保有・予約等の追加購入条件、cash不足とwarningの違いは[資本方針](../../../docs/portfolio-management.md)とCLI結果に従う。

後続配分は先行注文の人間報告をledgerへ反映し、新しい明示開始で最新cashを再確認する。比較対象が変わるmethod改訂後も新しい集合を選んでCAAを作り、適用可能な既存Reviewed Thesisは引き継ぐ。[資本方針](../../../docs/portfolio-management.md#reservation-and-warnings)に従い、無条件に企業調査を繰り返さない。

## 5. Operationを完了する

`operation show`の現在payloadを基に、固定selectionと成果を保持して`result / next`を記入し、`operation complete --payload <FILE>`へ渡す。新規完了があればその範囲の公開済みCAAを`capital_allocation_assessment` artifact/refで参照する。保有は検証済みdraftがOperationに残っていることを確認する。

これはL3提案完成・調査終了の記録であり、いずれのCAA結果でも人間の採否を完了条件にしない。人間未採否を`human_confirmation`へ補完せず、採否・broker操作・実約定の報告は別工程とする。未完了を含む選択集合の被覆を満たしたら、全未完了や0件も理由を残して終了できる。

必要なmonitoringは既存taskを確認して登録する。cloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。対象未確定、重要な入力矛盾、独立Review未完了、binding競合は解消するまで先へ進まない。
