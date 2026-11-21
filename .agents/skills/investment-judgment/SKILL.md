---
name: investment-judgment
description: 人間の明示開始から有限対象を選び、Company Research・Stock Assessment・Independent Review・新規または保有の提案を完成する。
---

# Investment Judgment

採否は[doctrine](../../../docs/doctrine.md)、根拠・価値・独立検証は[Stock Assessment](../../../docs/reference/stock-assessment.md)、行動・条件・内容Reviewは[Trade Proposal](../../../docs/reference/trade-proposal.md)へ。省略commandはinvestment-judgment配下。引数は`--help`、pathは返却値を使う。`<APP_DB>`の準備/保存後反映は[application手順](../../../jobs/OPERATIONS.md#application-db-を反映する)に従う。

## 開始と固定対象

既存Operationは旧形式も[IDで再開](#完了と再開)する。新規依頼は選定から提案へ進め、旧draft/runを新Selectionにしない。全候補Triage・人間の事前選定は要求しない。

```bash
uv run baibai-engine investment-judgment selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db <APP_DB> --runs-path stores/screening/runs.sqlite
```

通常の新規探索は、買い候補順ではなく**何が価格に対して安い可能性を作るか**を選ぶ。最初の出力の `repeat_command_args` に問いの `append_args` を付け、同じ公表物・同じcutoffで関連会社の根拠と反対説明を比較する。続きは `next_args`、個別に使った価格基準・期間・会社の一次根拠を確かめるには行の `detail_args` を同じ元コマンドへ付ける。対象のコードを先に知る必要はない。参照順を優先順位にせず、1問や全問の枠消化は不要。

報告資産の問いで株価水準によって調べる範囲を絞るときは、同じコマンドに `--discovery-question reported_assets --reported-pbr-max <倍率>` を指定する。赤字やテクニカル未生成でも、当時の財務自身に正の簿価と価格・株数基準が揃えば調査できる。別の記録に価格日の矛盾があれば比較不能とする。上限は閲覧条件で、買値や普遍的な基準ではない。各行の市場・業種中央値は**閾値適用前**の他社の観測。揃った株価日だけの比較と、簿価の期末・算出法・連単・会計基準まで揃う比較を分ける。標本なしを市場で補わず、違う事業や拘束資産・負担も調べる。値動きは下記の価格観測から別に読む。

銘柄指定は`--scope specified_stocks --ticker <TICKER>`（候補外可）、保有は`--scope review_positions`。new/holdingを混ぜず評価を変える問いで選ぶ。候補外比較は同commandへ返された`--screening-selection-id <ID> --compare-ticker <TICKER>`を渡し、依頼scopeを保って採用対象をdraftへ記す。

保有を定期点検するときは、同じ選定画面の `holding_review_history` から、前の正式項目の理由・観測条件と、その後に書いた振り返りを読む。これは最新版台帳を事実日まで再生した対象で、現在口座の確認ではない。全売却後の買い直し、元の保有期間を照合できない場合は、旧条件を今回の保有へ引き継がない。根拠を詳しく読むには元項目の `origin.proposal_id` と同じ締切を下記の `judgment-retrospective coverage` へ渡す。受領したことや後日メモがないことを条件達成・未達にしない。

前回の条件（配当の原資・普通株主に残る権利、採算、価格と代替など）から、今回確認する原典と問いを選び、採用対象の `research_question` に残す。元条件の誤りを訂正することと、企業が実際に変わったことを分ける。新しい根拠があれば、その時点の評価・独立Review・正式提案へ戻る。材料が増えていなければ同じ案の自動再発行や、原価からの損益だけでの変更はしない。

現在の発行物で入力不足を調べるときは `--discovery-question input_gap`、旧表示の外を比べるときは `outside_display` から同じ個社比較へ進む。旧レーン/除外のflagは固定した旧公表IDの照会だけ。仮説の反証、同業・地合いの影響、待機や別の資金用途によって判断が変わる比較を選び、[評価](../../../docs/reference/stock-assessment.md#uncertainty-and-decision)と[提案](../../../docs/reference/trade-proposal.md#wait-paths)へ渡す。入力未確定・執行不可と、価値は推定できても価格・損失・代替に見合わないことは別。

再訪は`company-research catalog/get`と[振り返り](#判断の振り返り)を有限に読む。catalogの続きはknown_atを固定しnext_after_tickerを使い、未review Research/review済みpair、空/unreadableを分ける。

候補外も含む価格面の探索は、同じGenerationの小セクションを私的JSONLへ読み、問いに合う対象だけを前述の比較へ渡す。全社の全モデルをL3へ投入せず、候補政策・順位は変更しない。

```bash
uv run baibai-engine technical-analysis saved \
  --generation-id <GENERATION_ID> --all-stocks \
  --section technical_analysis.moving_averages --section technical_analysis.price_momentum \
  --runs-path stores/screening/runs.sqlite > <PRIVATE_JSONL>
```

`--all-stocks`は当世代のstock plan、個社は繰返しの`--ticker`を使う。`--section MODEL.SECTION`で元の小セクションを選ぶ。相対強弱は`market_context.price_path_relative` / `market_context.relative`、変動・流動性は`market_context.price`と必要な`technical_analysis.bollinger_20bar`、タイミングの検討は`technical_analysis.rsi_14bar` / `technical_analysis.price_path`を読む。新しい分類や売買signalではなく、[期間・価格basis・限界](../../../docs/reference/model-observations.md#technical-analysis)を保つ。

`relative`の実在bar窓と`price_path_relative`の同一端点比較（R20は共通calendar）は区別し、旧世代で窓が未記録なら不明のまま扱う。

各行のGeneration・subject・モデルrevisionで元結果へ戻れる。`price_context`の株価日/鮮度と、選んだセクション内の期間・欠測を合わせて読む。`missing_models`は当世代planと照合し、`not_recorded_sections`をnullや0に置換しない。失敗したcommandの途中出力を完全な全社結果として使わない。

contextのidentity/時点/依頼を保つResearch Selectionを作り、対象・問い・理由・代替・未確認範囲を記す。全社モデル投入や単一対象の全期間backfillをせず、不足は必要な通常取得で補い経済的棄却で代用しない。新しい共通市場判断は[市場判断skill](../market-context-assessment/SKILL.md)へ。

```bash
uv run baibai-engine investment-judgment prepare \
  --selection-file <SELECTION_JSON> --db <APP_DB> \
  --workspace .cache/research/<ASOF> --runs-path stores/screening/runs.sqlite
```

prepareはResearch Selectionと開始材料をapplicationへ保存しdomain-inputs.yamlへ投影する。後発根拠はAssessment/Reviewへ追加しSelectionを変えない。完全結果は`operation --db <APP_DB> --format json show <ID>`のresearch_selection artifactが指すfull_result_refで読む。書込CLIの応答は操作ID・状態などのヘッダだけを返す。IDなしの`show`も履歴ヘッダのみ。固定証拠・進捗・結果の全文はIDを指定して読む。作業メモは `operation --db <APP_DB> checkpoint <ID> --note <NOTE>`。次手も更新する場合だけ `--next <NEXT>` を付ける。メモで固定入力・既存outcome・正式提案参照は変更できない。

個社の `external_assessment.monex_fiscal_eps` があれば、世代に固定済みの配信元・対象決算期・円/株・受領時刻を用いる。今期／来期のアナリスト予想、会社予想、目標株価、ローリング1年予想を取り違えない。欄なしは実世界での予想なしを証明しない。調査や提案のために原本の全件復元／再取得をせず、過去の判断へ後発データを足さない。

nativeは保存descriptorから[固定reader](../../../jobs/OPERATIONS.md#jquants-native-operations)へ進みsummary/latestで代用しない。候補/省略/未生成/欠測と時点・Reading・保存範囲は[観測reference](../../../docs/reference/model-observations.md#judgment-use)に従う。

### 会社に未関連の共有材料を調べる

業界・社会・市場の問いに必要なら、選定時のGenerationを固定して共有subjectを有限に探し、明示した1対象だけ読む。会社との関連や調査対象を自動追加しない。

```bash
uv run baibai-engine investment-judgment shared-context \
  --generation-id <GENERATION_ID> --runs-path stores/screening/runs.sqlite
uv run baibai-engine investment-judgment shared-context \
  --generation-id <GENERATION_ID> --subject <SHARED_SUBJECT> \
  --runs-path stores/screening/runs.sqlite > <PRIVATE_JSON>
```

これは指定runsからの追加探索で、applicationに固定した入力の[再開](#完了と再開)とは別である。元runsが不在・不読でも保存Selectionの断面はOperationから読めるが、未選定の共有材料まで保存されたとは限らない。追加探索の失敗を材料なしとせず、別Generationやlatestへ差し替えない。

一覧の続きは同Generationと`--after-subject <NEXT_AFTER_SUBJECT>`、件数は`--limit`で絞る。未記録scope・未生成・部分抽出を空の経済事実にせず、原subject/モデルrevision/時計・母集団・否定/条件・出典を保つ。追加材料は元Selectionを変えずResearch/Assessmentの根拠として扱い、会社への波及と価値仮定は別に検証する。

### 記事の原文へ戻る

共有subjectのcoverage/抽出状態/省略を確認し、[通常生成の範囲](../../../docs/architecture.md#現在の解析モデル)を全ニュースへ一般化せず重要原文だけprivateに読む。

```bash
uv run baibai-engine articles exact-evidence \
  --release <RETAINED_LOOKUP_RELEASE> --manifest-sha256 <LOOKUP_MANIFEST_SHA256> \
  --reference-file <EXACT_DESCRIPTOR_JSON> --cache <PRIVATE_CACHE> --output-dir <NEW_PRIVATE_DIR>
```

探索は同じrelease/manifest/cacheの`articles list`へsource・受領UTC月・公表期間を渡す。月ごとにprivate index.jsonから選び、続きは同条件のnext_after。公表日時不明は日時検索に入らない。

lookupは元lineageや旧release全体の再読でない。Research source.refに原典URL、used_forにexact descriptorを残し、日付・本文範囲・限定を後続/再開へ引継ぐ。自動exact照合は保証されず、原文を公開logへ転載しない。受領だけで独立性・変化・実施を認定しない。

## Company Research・Stock Assessment・Independent Review

同じworkspace/ticker/DB（保有はholding子workspace）で進める。

1. `stock-assessment-scaffold`へasofを指定し、[根拠とニュース](../../../docs/reference/stock-assessment.md#recent-news-research)を確認して執筆する。まず[統合した経済経路](../../../docs/reference/stock-assessment.md#analysis-domain-integration)を作り、Web提案の七観点にはそこへ効く支持・相反と不足を分解して記す。事業・成長・競争・懸念の前提を確かめるときは[外部見解との比較](../../../docs/reference/model-observations.md#public-research-reports)へ。主力成長と全社利益が食い違う時は[利益の橋渡し](../../../docs/reference/stock-assessment.md#segment-profit-bridge)を使う。PDF抽出失敗は原ページへ戻す。
2. `stock-assessment-evaluate <DRAFT>`で確認する。算術補助は`calculate`を使う。
3. `independent-review-scaffold`から別作業者が[独立検証](../../../docs/reference/stock-assessment.md#independent-second-pass)する。
4. `stock-assessment-evaluate <DRAFT> --review <REVIEW>`でpairを確認し、`stock-assessment-promote`で保存する。

探索で選んだ対象は、選択時の同世代の技術・市場・財務/成長の観測と留保が、事業価値・価格経路・待機のどこへ効いたか、変化なしも含めて記す。価格回復と新しい事業根拠を分け、需要到来の遅延は資金耐久・追加資本・普通株主帰属と併せて検討する。価格経路の不成立だけで事業仮説をbrokenにせず、小口や価格ストップで未成立の価値根拠を補わない。

通常の候補でも公開買付け・株式併合・監理指定があれば、継続保有時の価値をそのまま使わず、会社・取引所の原典で買い手に残る権利と入金時期を確認する。提案執筆の保存原本・現在上限の読み分けは[監理・整理と買値の根拠](../../../docs/reference/trade-proposal.md#listing-and-price-basis)を参照。

同じ会社の同日評価が既にあるときは、先に元の評価と独立Reviewを確認する。今回の問い・権利・価格をなお支え、新しい重要材料がなければ、草案を作り直さず同じpairを下記の`record-assessment`でこのOperationへ明示的に結ぶ。保有と新規で資金配分の結論が同じとは限らない。事業・価格条件・取得権が変わった時や、同じ一次資料でも株主取り分の計算・仮定を訂正した時は、同日でも再評価する。別の日の古い判断を当日評価に読み替えない。

scaffoldの`--from-research-id`は元日付/ID/hash付き素材コピー、`--from-assessment-id`はAssessmentの差分refresh。併用せず、コピーを評価済みとしない。

### 途中成果の保存と再訪

`company-research scaffold`はlatestまたは明示ID/hashからdraftを作る。調査範囲と追加/訂正/削除を編集し`company-research save`する。部分的理解のsnapshotとして検索不発も範囲/残る問いを保存でき、価値・価格・Reviewを捏造しない。

as_ofは情報境界で全内容の再検証日でない。元source/fact時刻を保ちコピーを再確認/削除の安全認定にしない。stale priorはlatestと統合して再保存し、同ID同内容は元保存時刻を返す。文字列参照の自動検証は保証しない。中断は理由とResearch ID/digestをrecord-unfinishedへ残し、部分保存をreviewed/完了としない。

予定開示後は[解消した問いと残る不明](../../../docs/reference/stock-assessment.md#event-resolution)を原典で更新し、旧「発表待ち」や未確認項目をそのまま今回の結論にしない。

## Trade Proposal

`investment-judgment status --workspace <WORKSPACE> --db <APP_DB>`とOperationを読む。評価保存とOperation束縛は別transactionで、再利用/束縛失敗だけなら`operation --db <APP_DB> --format json record-assessment <OPERATION_ID> --assessment-id <ID>`で回復する。別の作業場で再開して草案がなくても、`status`はこのOperationに結ばれた査読済み原本、または記録した未完理由を示す。原本が読めなければ参照を確認し、最新の同銘柄に差し替えない。執筆ファイルがあればその審査状態とOperationに実際に結んだ結果を区別する。ローカルのpairが別途発行済みでも、参照を明示して結ぶまではこの案件の結果にならない。完了案件の原本は書き換えない。

未完対象だけを記録し、自動補充しない。

```bash
uv run baibai-engine operation --db <APP_DB> --format json record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <REF>
```

通常の提案はcontextから作る。

```bash
uv run baibai-engine investment-judgment trade-proposal-context \
  --operation-id <OPERATION_ID> --proposal-id <NEW_PROPOSAL_ID> --out <DRAFT> \
  --db <APP_DB> --sqlite-path stores/market/market.sqlite
```

標準出力のcontextと任意の新規作業ファイルは同じ時点・IDの投影である。前回提案・本人報告、監理指定と関連届出の出典はcontextで確認し、今回の判断へ流用しない。作者は固定全対象の行動・理由・再調査条件を[Trade Proposal](../../../docs/reference/trade-proposal.md)に沿って書く。相対強弱を理由にするなら[反対の読み](../../../docs/reference/trade-proposal.md#fixed-price-comparison)で価値・採否・次の確認のどれが変わるかを残す。

`trade-proposal-check <DRAFT> --db <APP_DB>`の`unreviewed`は本文の事前確認で、正式保存ではない。評価のReviewとは別に、提案の内容Reviewerが本文とdigestを検証して[内容Review](../../../engine/src/baibai_engine/investment_judgment/trade_proposal/contracts.py)を記入する。`trade-proposal-publish <REVIEWED_DRAFT> --db <APP_DB>`は発行時点の事実を改めて確認する。

保有の利確・配当・業種集中が競合するときは[資本回収案の比較](../../../docs/reference/trade-proposal.md#holding-allocation)で、維持と、同じ現金を別の組合せから回収する案を固定終値・同じ権利窓で突き合わせる。現金目標がなければ作らず、差が採否を変えるものだけを正式本文に残す。

新規購入の待機は、現quote未満の指値で買う意思が完成した`buy / wait_limit`、追加情報を見て採否を決める未完の`defer`、経済比較を終えて現金維持を選ぶ`no_action`を分ける。指値だけが魅力的に見えるときは、[未成立・部分成立・全成立と現金の同一元手比較](../../../docs/reference/trade-proposal.md#wait-paths)で確かめる。MA回復待ちを買い指値やstop注文へ変換しない。確認後は残期間・今後分配・新根拠・現在価格を見直し、共通要因の重複、流動性、残cashと他案を既存の比較へ結ぶ。値上がりだけで許容価格を上げず、投稿上の損切り率や保有比率を共通規則にしない。

再調査条件は、原典で確かめた単位・指標・比較期間で書く。事業・権利の仮定を見直す必要があるときは、採否を左右する一つについて、次のどの公表資料で何を確かめるかを示す（元評価への参照でよい）。公表の有無や時期が未定ならそう書き、注文期限や別件の発表日で埋めない。たとえば「複数案件が不採算」なら、すでに黒字の部門を「黒字化待ち」としない。案件の採算が非開示なら未確認とし、同じ部門の前年同期との利益率比較とは分ける。

全対象が理由/参照付きunfinishedで、公開済みProposalも束縛参照もない場合だけProposalを省ける。resultに確認範囲/評価未完、nextに未解決の問い/再調査条件を記す。reviewedや未束縛の公開済みProposalがあれば通常経路を使い、経済的見送り/保有判断をunfinishedにしない。0対象は選定理由を残し空Proposalなしで完了する。

## 判断の振り返り

継続点検では、全社・全提案を開き直す前に、前回以降に同社のResearchまたは査読済み評価が保存された元提案の問いを読む。

```bash
uv run baibai-engine investment-judgment judgment-retrospective changes \
  --after '<PREVIOUS_CHECK_WITH_TIMEZONE>' --known-at '<THIS_CHECK_WITH_TIMEZONE>' --db <APP_DB>
```

返すのはAppへの受領差分であり、開示日や新事実ではない。読み取れたら次回は`next_after`から続ける。不明があれば同じ窓を保ち、読めた分から調べる。古い事実の再記述、違う仮説・権利、訂正された元提案を条件達成としない。

受領の有無にかかわらず、元項目が特定の決算で確かめるとした問いは[該当の開示](../../../docs/reference/stock-assessment.md#event-resolution)を原典で確認する。受領資料が同じ開示の同じ事実を扱っていれば一度の照合にまとめる。決算の公表だけで、同社の価格条件や市場・事業の別の前提まで開き直さない。該当項目の`coverage`を読み、当初の問いを変える根拠があれば下の`scaffold/save`で記録する。新しい材料がなければ同じメモは重ねない。

正式提案の振り返りは、先にその全項目を開く。発行時の理由・再調査条件（保留なら当初の障害と再開条件も）に並べて、後から何を比較し、次回に何を変えようと記録したかを読む。買いの価格は保存した指値と対象立会を組にして照合し、具体条件のない案に作者上限や後日の立会を補わない。

```bash
uv run baibai-engine investment-judgment judgment-retrospective coverage \
  --proposal-id <PROPOSAL_ID> --known-at '<CUTOFF_WITH_TIMEZONE>' --db <APP_DB>
```

記録なし、教訓だけ、比較しても未解決、作者が結論を書いた状態を分ける。どれも勝率・約定・新事実取得の証明ではない。たとえば元の資料を読み直して学んだことは有用でも、後日の企業実績を確かめたことにはならない。訂正された元提案を別の有効判断に数えず、同社でも別提案・評価だけの比較を付け替えない。確かめるべき項目は下のscaffoldへ、書かれた比較は返されたID/hashでgetし根拠を読む。次回変える点（または変えない理由）を定め、新しい根拠と価格で通常の調査・評価に戻る。未選定企業・未発行の調査や対照損益をここからは測れない。

同じ提案で採った案と見送った案を比べるときは、対象を入れ替えず、結果を見る前に始点と節目を揃える。各案の騰落と、元の投入額・単元・残る現金・その間に取得できる配当権を揃えた仮の配分は別々に示す。1単元に届かない案はその元手では現金のまま。各節目までに公表された資料だけで元の主張を再点検する。たとえば「売上と費用が一定なら粗利率何％で採否が変わる」という線を割っても、実際の売上・費用も変わっていれば、それだけで利益仮説が崩れたとは言えない。

当日限りの買い指値が届く価格だったかは、元の対象立会の正式発行後だけを見る。場中発行でも全日安値が指値より高ければ届いていない。安値が指値以下なら、取引所の時刻付き価格や、前場発行に対する後場の安値などで確認し、前後を分けられなければ不明とする。未到来の立会で代わりの日を使わない。指値以下の取引があっても本人の約定ではない。同社・同じ立会に複数の案がある比較は、各案を残し、発行件数と銘柄×立会の組数を併記する。

元条件の出発点が原典と違っていたら、後日の改善や悪化に数えず、元項目の比較メモでその解釈だけを訂正する。正しい次の問いを残し、一部事業の不確かさを会社全体へ広げず、他の事業のキャッシュで価格を説明できるかは別に検討する。

上期の上振れと、それを反映した通期上方修正を、先行き改善の別々の根拠にしない。上期実績は提案時に既知の直近の上期予想と比べ、下期に必要な利益の変化は、各公表時点で更新・据置が確認できる通期計画と同じ範囲の上期を組にして比べる。上期だけ先に修正して通期は精査中なら、「旧通期－新上期」は旧目標に必要な残額で、会社が示した新しい下期予想ではない。

旧判断と後日事実を照合する。企業知識の更新はResearch、現評価はrefreshへ分け、新Operation/Selection/Review/現在市場入力は不要。

```bash
uv run baibai-engine investment-judgment judgment-retrospective scaffold \
  --proposal-id <PROPOSAL_ID> --ticker <TICKER> --asof <ASOF> --db <APP_DB> --out <DRAFT>
uv run baibai-engine investment-judgment judgment-retrospective save <DRAFT> --db <APP_DB>
```

Assessment-onlyはproposal-id/tickerを`--assessment-id`へ替える。返されたexact原判断/Review/固定台帳を読み、節目理由・比較・lessonを書く。action/hash/eventを手転記せず、Assessment-onlyへ採否/注文を補わない。

Proposal由来の市場補助は、元Selectionのhashに一致する当時の投影だけを読む。旧記録の未保存と参照不可を分け、現在headや未記録時計で補わない。当時の反証・未知・伝達条件を見送り/待機理由と照合する補助であり、現在の鮮度や原判断の妥当性を再認定しない。

本人の非購入報告/提案訂正は元提案と別の受領記録として読む。提案全体への報告であり、個別銘柄の実行結果へ分配しない。補助contextは振返りの記録時刻までを返すだけで、当時既知やas_of内の事実を認定しない。報告/対象/受領時計を分け、対象時点未記録を推測せず、非購入をno_action・未発注・取消・未約定に変換しない。後から受領した報告は旧get/retryへ補完せず、新scaffoldで読み直す。

元主張の対象/条件/期と後日観測をexact文書内のsource/factで結び、比較理由を残す。支持/反証はその主張に限り、提案全体の勝敗ではない。同名source/旧supportedを今回確認にせず、不足は理由付きunresolvedとする。注文/約定/Reviewはcontext/lessonへ記し、架空Research sourceを作らない。台帳未記録はunknown、遅延報告は新scaffold/新IDで照合する。

後日値上がり/公表は当時の価格条件/未公表を反証しない。利益があってもbasis誤りは改善対象、受容済み不利経路の損失なら手順不変も正常。損益と判断過程を分け、次回変える/変えない確認と理由を残す。

as_ofは元発行日以降のJST情報境界で保存時計ではない。後日URL取得は当時既知/bytes不変を保証しない。同ID同内容は元記録、続きは新ID、訂正はcorrects_refとlessonの理由を持つ新draft。`judgment-retrospective list/get`で有限に再訪し、単一latestで結論にせずlessonを現在fact/買付条件へ転記しない。

## 完了と再開

```bash
uv run baibai-engine operation --db <APP_DB> --format json complete <OPERATION_ID> --proposal-id <ID> --result '<RESULT>' --next '<NEXT>'
uv run baibai-engine investment-judgment prepare \
  --operation-id <OPERATION_ID> --db <APP_DB> --workspace .cache/research/<ASOF> --force
```

完了時は現在の全対象・成果に一致する正式提案IDを明示する。既に訂正されたIDは使わず、最新への自動置換もしない。空選定または未発行で全対象未完の場合だけIDを省き、理由と次手を記す。同じID・結果・次手での再試行は元の完了を返す。buy完了には具体termsを要し、sell terms不足の理由付き調査終了を具体提案完成としない。completedは有限作業終了で、評価/提案完成・採否・注文・約定とは限らない。正本は`operation --db <APP_DB> --format json show <ID>`で読み、workspaceのincompleteと区別する。

Operationに保存された[Research Selection](../../../docs/architecture.md#store-authority)から再開し、selection-contextで選び直さない。forceは表示を再投影し執筆draftを保持する。DB commit後のfile失敗も返されたIDで再開する。別active workspaceを上書きせず、already_completedは戻さず、再依頼には新workspace/Operationを使う。取引/資金の報告は[Ledger](../ledger-record/SKILL.md)へ。

`company-research save`と`judgment-retrospective save`の`--out`は正本とは別の補助copy。保存後のexport失敗は終了codeを失敗のまま保ち、stdoutの`saved_output_failed`で保存済み参照と元記録時刻だけを返す。確認情報をprivateに保持し、同じcommand群の`get`へID/hashを渡して、新しい出力先に読み直す。新IDで保存し直したり、残った部分出力を完全な記録として使ったりしない。保存前の失敗では保存済みと報告しない。

## 提案訂正・人間の非購入報告

[訂正と本人報告](../../../docs/reference/trade-proposal.md#提案の訂正と人間の報告)に従いexact Proposalを読む。

```bash
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> correct <CORRECTION_DRAFT> \
  --expected-latest-id <ID> --expected-latest-sha256 <SHA256>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> report <HUMAN_REPORT_DRAFT>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> show <PROPOSAL_ID>
```

競合は読直しblind retryしない。同ID同内容だけ冪等で、保存時計はエンジンが付ける。別記録を読戻す。
