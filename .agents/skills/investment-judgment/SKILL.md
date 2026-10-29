---
name: investment-judgment
description: 人間の明示開始から有限対象を選び、Company Research・Stock Assessment・Independent Review・新規または保有の提案を完成する。
---

# Investment Judgment

## 開始と固定対象

既存Operationの続きは[完了と再開](#完了と再開)へ進み、新しいSelectionを作らない。旧形式の保存済みOperationもIDから再開する。旧draftや旧runを渡して新規開始しない。

クラウド正本へ反映する作業は、開始前に[application作業コピーの準備](../../../jobs/OPERATIONS.md#application-db-を反映する)を行い、DBと取得ETagを揃える。既存作業の再開ではその組を保持する。machineのpullはapplicationを含まない。

通常の「売買提案して」は`discover_new`、候補外も含む銘柄指定は`specified_stocks`、保有見直しは`review_positions`。同じ依頼で選定から具体提案まで進め、全候補Triageや人間による事前銘柄選択は要求しない。newとholdingを混ぜず、追加買付依頼は[現行方針](../../../docs/doctrine.md#investment-strategy)と照合する。

```bash
uv run baibai-engine investment-judgment selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db stores/application/baibai.sqlite --runs-path stores/screening/runs.sqlite
```

指定は`--scope specified_stocks --ticker <TICKER>`、保有は`--scope review_positions`を使う。ユーザーの目的から価値獲得の仮説を立て、生成済みモデル・保有・既存調査を比較する。必要な候補外の対照も同じ固定generationから有限に読む。機械順位の極端値だけを順に選ばず、capacityを弱い対象で埋めない。選定は購入順位でなく、次に調べる価値の比較である。価値差と損失経路、答えによって採否・価格条件が変わる大きさ、調査で答えが得られる見込みを既存のselection_summary・rationale・research_questionへ記す。開示が豊富なだけで優先せず、同じ未解決の問いを繰り返すだけなら他の対象と比較する。

新政策のselection-contextは保存済み候補政策の比較用要約と共有文脈を返し、表示省略は欠測や反対材料の不存在を意味しない。経路のraw値・符号・対象期と借入/現金/投資/希薄化等の反対材料を読み、候補入りを絶対的割安・健全・買付資格と扱わない。候補の順位低下だけを保有売却の理由にしない。未生成とpayload内の欠測を区別し、欠測を正常値へ補わない。prepareは選定した対象のモデルと政策本文を固定保存し、可変storeから再計算しない。

調査対象と採否は[投資戦略](../../../docs/doctrine.md#economic-return-policy)に照らして判断し、統合の進め方は下記のCompany Researchへ進む。

候補外や異なる価値獲得経路の少数の対照は、返された `screening_selection_id` を固定して同じcontextへ読める。`--compare-ticker` は `--ticker` と異なり、ユーザー指定銘柄・推薦集合・深掘り対象を変更しない。未生成のモデルは各社の `not_generated` に残る。実際の対象は比較後にdraftへ選び、候補外を毎回入れるノルマにはしない。

```bash
uv run baibai-engine investment-judgment selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --screening-selection-id <SELECTION_ID> --compare-ticker <TICKER> \
  --db stores/application/baibai.sqlite --runs-path stores/screening/runs.sqlite
```

以前の候補外・非保有企業を再訪するときは、保存済みResearchとAssessmentの企業一覧を有限に読む。これは買付順位ではない。続きのページは返された`known_at`を固定し、`--after-ticker <NEXT_AFTER_TICKER>`を渡す。気になる企業だけ上の`--compare-ticker`へ接続し、全履歴を自動投入しない。

```bash
uv run baibai-engine investment-judgment company-research catalog \
  --asof <ASOF> --known-at <INPUT_KNOWN_AT> --limit 20 --db stores/application/baibai.sqlite
```

`selection-context`の既存研究は薄い表示で、未review Researchと過去のreview済みAssessmentを分ける。`unreadable`を「調査なし」にせず、briefの省略を反証なしと扱わない。全文は`company-research get --research-id <ID> --sha256 <DIGEST>`または`--assessment-id <ID>`で読む。通常prepareは選定対象だけの開始材料をSelection V4へ固定し、再開時にlatestへ付け替えない。調査中に追加した根拠はAssessment V1へ必要部分をコピーしてReviewする。開始時Selectionは最終評価の全provenanceではない。

以前判断した銘柄を再調査するときは、最近保存された振り返りを有限に読み、今回の問いに関係するものだけexact getする。空一覧は正常で、全社の登録や全履歴読取は要求しない。`has_more`なら必要に応じてlimitを広げる。lessonは現在の企業事実・買付条件ではなく、今回確認する問いへ戻す材料である。再開時に追加で読む記録を元Selectionの開始入力だったように扱わない。

```bash
uv run baibai-engine investment-judgment judgment-retrospective list \
  --ticker <TICKER> --limit 20 --db stores/application/baibai.sqlite
uv run baibai-engine investment-judgment judgment-retrospective get \
  --retrospective-id <ID> --sha256 <DIGEST> --db stores/application/baibai.sqlite
```

低PERなら利益の反復性、過去平均なら回復経路、資産proxyなら株主への帰属・解放、成長なら追加投資の見返りを比べる。候補抽出の名称をその企業の価値獲得経路と決めつけず、[経済性の問い](../../../docs/reference/stock-assessment.md#経済性から問いを選ぶ)へつなぐ。重要でも調べて確定しない未来は、調査の無期限延長でなく条件付き評価へ残す。

企業成長の範囲は保存モデルのrequested_start/endとcoverageで確認する。通常生成は全保持範囲であり、長期の読取は下記の表示期数と完全結果を使う。prepare中に生成範囲を変えたり、最新原本へ付け替えたりしない。[期間・欠測と入力固定](../../../docs/reference/company-growth.md)に従う。

新規draftはschema_version=2とし、contextのgeneration_id・screening_selection_id・Investment Judgment日・cutoff・requestを引き継ぐ。対象と問い、選定理由、比較した代替と未確認の範囲を記し、全社モデルをLLMへ一括投入しない。指定/保有は候補外でも調べられ、候補内membershipを購入資格にしない。

機械入力の不足は経済的棄却と分ける。固定generationの未生成・欠測理由と該当sourceの受領状況を確認し、必要な通常取得で補い、得られないモデルや値は未生成・欠測のまま判断へ残す。単一対象の不足を全期間backfillで埋めない。新たな共通市場判断を今回作る場合は[市場判断skill](../market-context-assessment/SKILL.md)を先に完了する。

```bash
uv run baibai-engine investment-judgment prepare \
  --selection-file <SELECTION_JSON> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --runs-path stores/screening/runs.sqlite
```

Operationの固定入力を`domain-inputs.yaml`へ投影する。新しい資料は評価のsourcesへ加え、開始入力へ挿入しない。時点の制約は[入力の意味](../../../docs/reference/data-sources.md)と[Reading](../../../docs/reference/market-context.md#reading)に従う。

企業成長の通常表示と完全結果の関係は[Investment Judgment投影](../../../docs/reference/company-growth.md#investment-judgmentへの接続)に従う。長期の問いにはprepareの `--company-growth-periods 24` または `all` で表示だけを広げ、`operation --db <APPLICATION_DB> --format json show <ID>` の `payload.artifacts` 内の `kind=research_selection` を読み、`full_result_ref` から完全結果を辿る。既存workspaceの切替は[再開手順](#完了と再開)の `--force` を使う。

## Company Research・Stock Assessment・Independent Review

1. `investment-judgment stock-assessment-scaffold --workspace <WORKSPACE> --ticker <TICKER> --asof <YYYY-MM-DD>`で作る。返された`assessment_draft`を編集し、以下の`<DRAFT>`にそのpathを使う。保有は対象のholding子workspaceを使う。[Stock Assessment](../../../docs/reference/stock-assessment.md)から判断を変える経済的な問いを選び、一次資料へ戻る。[直近ニュース・カタリストの確認](../../../docs/reference/stock-assessment.md#recent-news-research)をCompany Researchの基本手順として行う。

   [解析モデルの統合](../../../docs/reference/stock-assessment.md#analysis-domain-integration)に従い、重要な支持・相反・不足を個社の仮定へ接続する。価格contextもその一材料であり、必要なら同端点の市場比較とニュースを照合する。価格注記だけを調査の起点に固定せず、全シグナルの追加調査を義務にしない。
2. [条件付き価値](../../../docs/reference/stock-assessment.md#scenario-arithmetic)を組み立て、数値計算には`investment-judgment calculate <INPUTS>`を使える。PDFの抽出が空・崩れた場合は原ページへ戻り、記載なしやゼロとしない。
3. `investment-judgment stock-assessment-evaluate <DRAFT>`で確認する。`investment-judgment independent-review-scaffold --workspace <WORKSPACE> --ticker <TICKER>`からReviewを準備し、作者と別の作業者が原典・仮定・算術・反証を検討する。作者の入力や出力のコピーを独立検算としない。[不確実性の受容](../../../docs/reference/stock-assessment.md#uncertainty-and-decision)に従い、未知を一律保留にせず、受けるリスクと見返りの根拠を確認する。
4. `investment-judgment stock-assessment-evaluate <DRAFT> --review <REVIEW>`で一組を確認し、`investment-judgment stock-assessment-promote --workspace <WORKSPACE> --ticker <TICKER>`で保存する。core変更時はReviewを取り直す。数値未確定でも所見と理由を独立検証した評価は完成でき、調査自体の不足は未完了とする。

別日の当日算術は`stock-assessment-scaffold --from-assessment-id <ID>`から[差分refresh](../../../docs/reference/stock-assessment.md#assessment-refresh)を作り、新しいReviewを取る。

### 途中成果の保存と再訪

評価に不足する段階でも、`company-research scaffold --ticker <TICKER> --asof <ASOF> --db <APPLICATION_DB> --out <DRAFT>`から独立Researchを保存できる。既存latestの出典・事実・仮説・問いをコピーする。特定の元記録なら`--from-research-id <ID> --from-sha256 <DIGEST>`を指定する。`investigated_scope`へ今回確認した範囲を記し、必要な追加・訂正・削除を行って`company-research save <DRAFT> --db <APPLICATION_DB>`へ渡す。検索不発も試した範囲と残る問いで保存でき、価値差・買付価格・Reviewを創作しない。

保存は差分メモでなく、その時点で保持する部分的な企業理解のsnapshot。recordの`as_of`は情報境界であり、全内容の再確認日ではない。元source/factの日付と取得時刻を今日へ変えず、引き継いだsupportedを本日再検証済みと扱わない。scaffoldはコピーだけを保証し、著者の削除が意味的に安全かは認定しない。stale priorの競合はlatestを読み直して著者が統合し、blind retryしない。同ID同内容の再送は元の保存時刻を返す。

Research保存はOperation完了やreviewed outcomeではない。中断するときは既存`record-unfinished`へ理由とResearch ID/digestを参照として残せるが、その文字列参照を自動検証済みリンクとは扱わない。

## Trade Proposal

statusとOperationを読む。`stock-assessment-promote`は保存評価をOperationへ自動束縛する。別途保存済みの評価を今回へ再利用する場合、または評価の保存後に束縛だけが失敗した場合は`operation record-assessment <OPERATION_ID> --assessment-id <ID>`を使う。評価の保存とOperationの束縛は別transactionである。未完了は理由と参照を記録し、対象を自動補充しない。

```bash
uv run baibai-engine operation --db stores/application/baibai.sqlite record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <SOURCE_OR_WORKSPACE_REF>
uv run baibai-engine investment-judgment trade-proposal-context \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --sqlite-path stores/market/market.sqlite
uv run baibai-engine investment-judgment trade-proposal-scaffold \
  --operation-id <OPERATION_ID> --proposal-id investment-proposal-<UUID> --out <DRAFT>
```

[Trade Proposal](../../../docs/reference/trade-proposal.md)に従い全固定対象を被覆する。0対象では空Proposalを作らず、選定理由を保って終了する。

新規買付は[指値と待機案の選択](../../../docs/reference/trade-proposal.md#limit-selection)に従い、同じ固定入力で候補の数量・支出・returnと待機案を比較し、選択理由と未約定後の再確認を記してからcheck・独立Reviewへ進む。

```bash
uv run baibai-engine investment-judgment trade-proposal-check <DRAFT> --db stores/application/baibai.sqlite
uv run baibai-engine investment-judgment trade-proposal-publish <REVIEWED_DRAFT> --db stores/application/baibai.sqlite
```

売却は[保有継続と回収](../../../docs/reference/trade-proposal.md#holding)に従い、回収・保有継続・回復待ちの理由を内容Reviewへ渡す。残存見返り不足による売却では、実quoteと作者下限の比較をcheckの算術と照合する。重要仮説の破綻による売却では、未確定・無効となったvaluationの下限算術を必須にしない。

checkのvalidとreview_bindingを別に読み、作者と別の作業者が正規modelのdraft_sha256へ内容Reviewを束縛する。[Review・保存・preview](../../../docs/reference/trade-proposal.md#publication-review)に従い、入力事実が変わればsnapshotとReviewを取り直す。

## 判断の振り返り

特定の旧判断と後日事実を照合し、次回への学びを独立に残したいときだけ使う。企業知識の更新だけならResearch、現在の再評価ならrefreshでよく、同じ内容の再記入は不要。新Operation・Selection・独立Reviewや現在の市場入力を振り返りの前提にしない。

```bash
uv run baibai-engine investment-judgment judgment-retrospective scaffold \
  --proposal-id <PROPOSAL_ID> --ticker <TICKER> --asof <ASOF> \
  --db stores/application/baibai.sqlite --out <DRAFT>
uv run baibai-engine investment-judgment judgment-retrospective save <DRAFT> \
  --db stores/application/baibai.sqlite
```

Assessmentだけなら`--proposal-id`と`--ticker`の代わりに`--assessment-id <ID>`を使う。scaffoldはexact参照・当時の判断とReview・固定した関連台帳を一度に返す。`context`を読み、`draft`内の節目・比較・lessonだけを執筆する。元actionやhash、台帳eventを手で転記しない。Assessment-onlyに採否・注文を補わず、deferを経済的な棄却へ読み替えない。

- `milestone`には今回選んだ決算・出来事とその理由を短く書く。`original_claim`は元の対象・条件・期間を保持し、`observed`に今回の観測、`reason`に照合の範囲と理由を分ける。三状態は選んだ主張の支持・反証・未解決であり、提案全体の勝敗ではない。
- supported/refutedは保存済みResearch又はAssessmentへのexact `reference`と、その文書内の`source_ids`（必要なら`fact_ids`）を`citations`へ付ける。複数文書を引用でき、同名source IDもrefごとに区別する。原典で比較できる企業仮説・再開条件を対象にし、Researchの既存supportedを今回の確認結果としてコピーしない。根拠が足りなければ、理由付きunresolvedだけでも保存できる。
- 注文・約定やReview完成等の運用条件は固定contextとlessonへ記し、架空のResearch sourceへ転記しない。比較なしでも`lesson.next_time`と`rationale`に意味があれば保存できる。台帳未記録はunknownであり、未約定を推測しない。新たな遅延報告は旧記録へ後付けされず、必要なら新しいscaffoldから別IDで照合する。
- lessonは次回に変える・変えない具体的な確認と理由にする。「当時1,100円でPmax1,000円」は後日上昇しても反証されず、「当時未公表」は翌月の公表で誤りにならない。後日初めて判明した材料と当時の見落としを分ける。利益が出ても株数basisの誤りは改善対象、明示して受け入れた不利経路での損失だけなら手順変更なしも正常である。実現損益と判断過程を同じ採点にしない。

`as_of`は元発行日以降の情報基準日（JST）で、保存時計とは別。古い資料を後日取得して引用できるが、当時既知だったことや外部URLの原本bytes不変を保証しない。同ID同内容の再送は元記録を返し、後日の続きは新IDにする。誤記の訂正は新draftの`corrects_ref`へ旧ID/digestを指定し、lessonに訂正理由を書く。一覧ではorigin・節目・保存日・訂正先を読み、単一latestを唯一の結論にしない。

## 完了と再開

公開済み提案を`operation record-proposal <OPERATION_ID> --proposal-id <ID>`へ記録し、`operation complete <OPERATION_ID> --result '<RESULT>' --next '<NEXT>'`で完了する。完了は保存済みの固定Selection・成果・参照を保ち、現在の成果と公開済み提案のscopeを照合する。buyを含む完了には具体termsが必要。sellのterms不足は理由付きで調査工程を終了できるが、価格・数量まで完成したと報告しない。人間の採否・broker操作・実約定を完了条件へ補完しない。

```bash
uv run baibai-engine investment-judgment prepare \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --force
```

再開は保存入力から投影を復元し、元generation・旧runやmutable storeへ付け替えない。forceは生成投影を修復し、執筆済みdraftを保つ。commit後のfile失敗も返されたOperation IDから再開する。別activeを上書きせず、完了済みへ追記しない。同文の本当の再依頼は新workspaceと新Operationで扱う。

後続調査は現在の成果を確定して次Operationへ進む。実際に報告された注文・資金は[Ledger Record](../ledger-record/SKILL.md)、本番反映は[application反映](../../../jobs/OPERATIONS.md#application-db-を反映する)へ進む。monitoringは同じ問いの既存taskを確認して記録する。
