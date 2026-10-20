---
name: research
description: ユーザーの明示開始で有限対象を調査し、独立Reviewを経て新規・保有提案を完成する。
---

# Research

## 開始条件

通常の「投資提案して」は `discover_new` とし、AIが保存分析・候補を比較して対象を選び、同じ依頼で深掘り、独立Review、Investment Proposalと具体条件まで進める。全候補Triage発行やユーザーによる銘柄選択を要求しない。銘柄指定は候補外も含む `specified_stocks`、保有見直し・売却提案は売却を前提にしない `review_positions` という同じResearchの別入口とする。保有の深掘りを通常探索へ混ぜない。全保有quoteの更新や無関係な入出金の再確認を企業調査の前提にせず、提案条件に必要な現在入力は第3・4節で確認する。

既保有への新規追加は現在方針で禁止されている。意図が保有見直しと明確ならそのscopeで開始し、明示的な追加購入依頼には方針を説明する。不明な場合だけ質問する。

有限集合と開始要求はOperationが所有する。[Research進行](../../../docs/reference/analysis-operations.md#明示開始と固定対象)の被覆・完了・再開に従う。深掘り上限は実行policyでありノルマではなく、0件も選定理由を残す。残りの依頼範囲をsummaryで説明し、追加調査は現在の成果を確定して次Operationへ進める。新しい費用・範囲の承認が必要な場合だけ確認する。日次AI自動起動は行わない。

## 1. 選定してWorkspaceを準備する

```bash
uv run baibai-engine research selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db stores/application/baibai.sqlite --runs-path stores/screening/runs.sqlite
```

指定銘柄は `--scope specified_stocks --ticker <TICKER>`、保有は `--scope review_positions` とし、必要なら指定tickerを付ける。通常依頼ではユーザーの目的と比較したい戦略仮説を読み取り、保存済みの機械候補を調査の入口として比較する。contextのrun参照、research_as_of、input_known_atとrequestをdraftへコピーし、比較した選定理由・調査の問い・主要リスク・今回の範囲と確認しなかった範囲を`selection_summary`と対象ごとの`rationale`へ記す。構造はCLIのhelpとselection modelが正本である。現在の三領域・保有・既存調査を読み、弱い対象で上限を埋めず、脱落を自動補充しない。

selection-context schema2の`candidates`は同runのraw Stock Analysisで、`candidate_discovery_method`と各行の`candidate.reasons`が機械選抜の方式とApproach内rankを表す。候補0件は完成したrunの正常結果である。機械候補は全投資機会ではない。通常の`discover_new`でも、依頼から導いた重要な仮説が候補に表れない場合は、同じ固定runの候補外分析を必要な範囲だけ確認できる。候補外の指定・保有対象も同runの全分析から読む。AIの選定理由を機械rankと混ぜず、新しい暗黙の順位や配当利回り・機械E[r]による購入条件を作らない。normalized gapの0とnull、rawの欠落とnullを保持し、候補一覧の別保存・再発行は行わない。

必要時の読取には、readyなcontextの`run_revision_id`を使う。`run-show`は保存revisionの読取であり、Researchのcutoffは自動照合しないため、出力の`run_revision_id`がcontextの同名field、`asof_date`が`analysis_as_of`と一致し、`asof_date <= research_as_of`、`run_at <= input_known_at`であることを確認する。不一致なら選定を止めてcontextを取り直す。

```bash
uv run baibai-engine screening run-show \
  --run-revision-id <CONTEXT_RUN_REVISION_ID> \
  --runs-db stores/screening/runs.sqlite --format json \
  --output-path <PRIVATE_RUN_JSON> > /dev/null
```

全runをLLMへ一括投入せず、仮説に関係する保存行をローカルで有限に読み、候補内の対照と欠測・流動性も確認する。候補外を選んでも一般依頼のscopeは`discover_new`のままとし、`candidate.reasons`の空を経済的な棄却へ読み替えない。調査対象はcontextのcapacity（現行最大3件）内で固定し、選定理由、比較した代替、未確認の範囲を既存Selectionへ残す。一時的なフィルタやticker順を恒久の選抜規則にしない。

inputs_requiredは経済的見送りではない。既存 `screening ticker-profile`、`verify-cache-coverage`、必要な `bootstrap-cache` / `screening run` と一次資料確認で補完し、同じ依頼でcontextを取り直す。Research factsは保存済みXBRLの再抽出なら `extract-edinet-facts`、原本取得が必要なら `refresh-edinet-facts` を使う。未初期化storeの扱いは[初期化手順](../../../batch/OPERATIONS.md#edinet-research-facts)に従い、通常のskipを取得・抽出成功と扱わない。通常の認可済みread取得に再許可を要求しない。新課金・権限変更・本番反映は[Ops Maintenance](../ops-maintenance/SKILL.md)の別境界であり、単一銘柄の不足を無断の全期間backfillで埋めない。標準補完後も機械分析を得られなければInputGapに試したsourceと不足理由を残し、一次資料で企業評価を進める。重要fact自体を得られない対象は未完了として扱う。

```bash
uv run baibai-engine research prepare \
  --selection-file <SELECTION_JSON> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --sqlite-path stores/market/market.sqlite
```

Operation内の一つのsnapshotが固定入力の正本であり、`domain-inputs.yaml`は削除・再生成可能なAI読取投影である。選択済み対象だけの機械値・市場・外部評価と、scope/summary/選定理由/問い/リスクを読む。保有子は `domain_inputs_ref` で親入力を共有する。観測・機械anchor・保存判断・今回の仮定を分け、materialな環境変化だけ企業評価と提案へ接続する。

`domain-inputs.yaml`の`analysis_source`で固定runの日付・rules hash・E[r] modelを確認する。保存済みE[r]較正文脈を参照する場合だけ、`research calibration-context --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite`を明示実行する。結果は固定runのmethod identityと照合した読取専用の歴史分布であり、欠損・期限切れ・不一致は理由付き利用不能とする。候補順、Selection、Research進行、案件ごとの要求率や購入採否へ自動接続しない。分母、未評価、formal evidenceの限界を合わせて読み、今の企業見積りや現在の市場判断へ読み替えない。

```bash
uv run baibai-engine research prepare \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --force
```

再開はOperationの保存入力から復元し、元runやmutable storeを再読しない。scope/対象/理由/source/cutoffは固定。`--force`は生成投影を修復し、執筆済みdraftを保持する。DB commit後のfile失敗も返されたOperation IDで再開できる。completedの再送はalready_completedで成果を追記しない。本当に再依頼された同じ文は、新workspaceで別Operationとして開始できる。

分析日が前日でもResearch日は当日。開始入力を当日の分析へ付け替えず、Assessment/Review/Proposalは実際の正式評価日で作る。翌日再開でも開始snapshotは不変であり、当日の価格・cash・保有・注文・正式評価条件は再確認する。

<a id="company-research"></a>

## 2. Caseごとの企業評価を確定する

保存済み年次のセグメント・負債満期は[固定releaseのResearch query](../../../docs/reference/market-lake.md#edinet-research-query)から参照できる。欠測・書類状態未確認の場合は原典確認へ戻る。

選択済み入力の`cash_flow`と3FY平均EPSベースの機械倍率は、[Valuationの意味とCF文脈](../../../docs/reference/valuation-metrics.md#researchへ渡すcf文脈)に従って読む。
機械snapshotの実績・予想年間DPSと採用`dividend_basis`が食い違う場合は、対象期、普通・特別／記念配当の内訳、還元方針を原典で確認する。これらは計算用の株式basisへ正規化した値であり、予想の持続性や上限適用理由を値だけから決めない。

小型であることやFCFが0・負であることだけで経済的見送りにしない。サイズ・ADVは執行の制約、負FCFは維持投資/成長投資・運転資金・回収・資金余力の問いへ接続する。CFのsourceと品質・期間を保ち、半期の`approximated`値を通年実績と読み替えず、資料で確認すべき重大な不足を免除しない。

短い価格bar期間も一律の見送り条件にしない。`price_history_span_days`は利用可能な価格履歴の日数proxyであり、実上場日や財務履歴ではない。企業評価に必要な財務期間は一次資料で確認し、短い自己価格レンジを長期の価値根拠と読み替えない。

候補のValuation Approachは調査の入口であり戦略ラベルではない。複数の価値実現仮説が重なるなら、経済経路・時期・反証を分けて検討する。[Market Context読取](../../../docs/reference/market-context.md#market-context-reads)の比較基準日・母集団を確かめ、重要な環境変化が企業の需要・費用・資金と将来cash、割引率・要求収益、市場の織込み・流動性のどの経路に効くか区別する。cashへの影響がないだけで除外せず、個別相対値を企業価値認定へ置き換えず、同じリスクを利益・倍率・要求率で無説明に重複計上しない。[External Assessmentの保存済み照会](../../../docs/reference/market-lake.md#external-assessment-query)の目標値・rating・scoreは、元評価者、metric、対象期、株式basis、元更新日と前提が判る範囲で比較する。重要な相違だけを採用・反証・保留または参考扱いの理由とともに既存の仮説・価値差へ記す。比較不能な値を無理に採用せず、同原評価元の重複配信を独立票としない。非重要な観測、観測はあるが比較条件が未確認、観測自体が未取得を区別する。

この節は企業の調査・算術・独立Reviewを扱う。保有を扱う場合は同じ有限Operationの保有workspaceを使い、新規買付へ混ぜない。

1. `research assessment-scaffold --workspace <WORKSPACE> --ticker <TICKER> --asof YYYY-MM-DD --out <WORKSPACE>/<TICKER>/assessment-draft.yaml`で新しいdraftを作る。保有はholdings/<ticker>子workspaceを指定する。明示mappingされたplaybookの問いを一次資料で検討し、[事業モデル別調査](../../../docs/reference/business-model-research.md)は指定された試行の補助として使う。
2. [投資目的とリスク許容](../../../docs/doctrine.md#investment-strategy)と[Stock Assessment](../../../docs/reference/stock-assessment.md)に従い、事業・資本配分が生む価値と市場価格との差を分け、重要な観測が将来cash・終価の評価・要求率・実現時期のどこを変えるかを既存Case/Findings/Projectionへ一続きに記す。Base/Downsideと反証を各経路へ結び、比較不能な外部値をfactや自己終価へ変換しない。全入力の採否や仮説の件数は要求しない。新しい評価の要求率は[要求年率方針](../../../docs/portfolio-management.md#required-return-policy)に従い、現金維持との比較、下振れ・期間・予測の不確実性を踏まえて作者が明記し、既存calculationへ経済的理由を書く。価格を通すために率を合わせない。scaffoldのnullを0・空・自動推計で埋めず、既存評価の率を現在policyで置換しない。
   利益の1株換算には直近の自己株控除株数を起点とし、重要後発事象と評価日までの一次開示で増資・自己株処分・消却を照合する。資料・単位・期間を確認し、比較表の数値は行だけでなく年度見出しと当期・前期の列を一組で読む。重要な事実の欠落と合理的な見積りの幅を分ける。利益・倍率・要求率で同じリスクを重複控除せず、経済経路が違う影響は区別する。
   ratioとProjectionは[Draft算術](../../../docs/reference/stock-assessment.md#draft-calculation)の`research calculate <INPUTS>`を使えるが、企業見積りと算式は一次資料から別途確認する。
3. `research assessment-evaluate <DRAFT>`で公開内容と原価格の計算資料を確認する。Review未添付だけのreview_requiredもexit 2であり、公開前にはReviewが必要である。他のerrorを無視する許可ではない。
4. `research assessment-review-scaffold --workspace <WORKSPACE> --ticker <TICKER>`で現在coreに束縛するReview draftを作り、作者と別の作業者がsource・算術・反証を確認する。Review側も原本の年度見出し・当期/前期列と算術期間を自分で照合し、終期の権利落ち後株価・未入金配当権利・期間内現金受取を分けた独立入力でcalculateを呼び、recalculated_projectionsへ渡す。作者の抽出値・入力・出力をコピーして独立検算としない。要求率の経済的根拠と、重要なマクロ経路と外部相違について、原典が個社仮定を支える範囲、比較不能な条件、結論を覆す反例を独立に見る。review_scopeに再確認した範囲と保存済み根拠を読み直した範囲・限界を書く。`assessment-evaluate <DRAFT> --review <REVIEW>`で一組を確認し、core変更時はReviewを取り直す。
5. supported/uncertain/unsupportedを確定し、`research assessment-promote --workspace <WORKSPACE> --ticker <TICKER>`で評価とReviewを原子的に公開する。supportedは購入採用ではなく、cash不足や現在価格だけで評価結論を書き換えない。valuation unresolvedでも所見と理由を独立検証した評価は完成できる。調査そのものの根拠不足は未完了として記録する。

同日には同じimmutable pairを提案が参照できる。翌日以降に当日算術を使う場合は`assessment-scaffold --from-assessment-id <ID>`で差分draftを作り、価値・残る期間・今後分配・株式単位を確認する。旧source/fact日付は不変で、horizonと今後分配・refresh理由は未入力から記す。旧期間を毎日延長せず、受取済み分配を今後分へ持ち越さない。以前の根拠を再利用できるが、今回の全coreへ新しい独立Reviewを取る。

## 3. 全対象の行動と具体条件を比較して公開する

`research status`と`operation show`で成果を確認する。new/holdingともpromoteがAssessment参照をOperationへ記録する。適用可能な保存評価を使う場合は`operation record-assessment <OPERATION_ID> --assessment-id <ASSESSMENT_ID>`で明示して束縛する。未完了対象は次で理由と確認した参照を残し、代替対象を補充しない。

```bash
uv run baibai-engine operation --db stores/application/baibai.sqlite record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <SOURCE_OR_WORKSPACE_REF>
uv run baibai-engine research proposal-context \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --sqlite-path stores/market/market.sqlite
uv run baibai-engine research proposal-scaffold \
  --operation-id <OPERATION_ID> --proposal-id investment-proposal-<UUID> --out <DRAFT>
```

固定scopeの全対象と完成/未完了をProposalへ投影し、保存入力を固定する。全対象についてbuy/hold/sell/no_action/deferを区別し、未調査はdeferとする。0対象では空Proposalを作らず、選定理由を保持して第5節へ進む。完成対象の価値を生む経路、Downside・時期・反証と現金維持等の代替を比較し、重要な外部相違をどう扱ったかは参照評価へ辿れる理由にする。未完成を経済的な見送りへ混ぜない。詳細は[Investment Proposal](../../../docs/reference/investment-proposal.md)が所有する。

buyは実quoteと試算limitを分け、Pmax以下の指値待ちも許される。closeを架空の安い値へ変えず、価格以外の仮説・保有・予約・権利単位・cash等のgateを確認する。価格・数量・次session・条件を記し、current limitは即約定を保証しない。分類や共通gridの確認不足は具体terms未確定として残す。holdは今回の仮説維持確認と当日の収益算術を分け、算術不足だけで仮説を無効にしない。brokenを確認したsellはquote不明でも経済判断を残せるが、具体売却条件が未完了であることを示す。

```bash
uv run baibai-engine research proposal-check <DRAFT> --db stores/application/baibai.sqlite
```

確認したdraft_sha256に作者と別の作業者が内容Reviewを行う。価格・数量・cash・scope・warningもhashに含まれる。修正時は再Reviewし、一致した原稿を公開する。

```bash
uv run baibai-engine research proposal-publish <REVIEWED_DRAFT> --db stores/application/baibai.sqlite
```

公開は提案を保存する工程であり、予約・注文・約定や人間の採用を作らない。

## 4. 保存条件と現在preview、人間判断

`research proposal-preview --proposal-id <ID>`で現在の再試算を読む。保存時のinputs/termsを現在quote/cashへ上書きせず、現在の不足・blocked reasonと再試算を別fieldとして示す。checked_atの経過だけを入力driftにせず、実入力値の変化を確認する。

現在再試算が使えなくても、保存評価と当時の条件付き算術は閲覧できる。terms未確定のbuy/sellは理由ある部分結果として保存できるが、価格・数量まで具体提案が完了したと報告しない。Proposal公開とOperation完了はいずれも人間採用を意味しない。

本人の採否とbroker操作を待ち、実際に報告された事実だけを[Ledger Record](../ledger-record/SKILL.md)へ渡す。後続提案は注文報告をledgerへ反映した最新cashを再確認し、新しい明示開始で進める。完了済みOperationへ成果を追記せず、適用可能な保存評価と根拠を次Operationへ引き継ぐ。

## 5. Operationを完了する

`operation show`の現在payloadを基に固定selection/outcomesを保持してresult/nextを記し、`operation complete <OPERATION_ID> --payload <FILE>`へ渡す。公開済みProposalは`operation record-proposal <OPERATION_ID> --proposal-id <ID>`でID/hash参照を記録する。本文やhold draftをOperationへ再保存しない。

固定対象の被覆と公開済み提案を確認する。buy/sellの具体terms不足は未完了理由を明示し、具体提案完成と区別する。0対象は理由ある選定結果を保って終了できる。L3提案完成と調査終了を記録し、人間の採否・broker操作・実約定を完了条件へ補完しない。

必要なmonitoringは既存taskを確認して登録する。cloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。対象未確定、重要な入力矛盾、独立Review未完了、binding競合は解消するまで先へ進まない。
