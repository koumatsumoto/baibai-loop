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

銘柄指定は`--scope specified_stocks --ticker <TICKER>`（候補外可）、保有は`--scope review_positions`。new/holdingを混ぜず評価を変える問いで選ぶ。候補外比較は同commandへ返された`--screening-selection-id <ID> --compare-ticker <TICKER>`を渡し、依頼scopeを保って採用対象をdraftへ記す。

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

prepareはResearch Selectionと開始材料をapplicationへ保存しdomain-inputs.yamlへ投影する。後発根拠はAssessment/Reviewへ追加しSelectionを変えない。完全結果は`operation --db <APP_DB> --format json show <ID>`のresearch_selection artifactが指すfull_result_refで読む。

nativeは保存descriptorから[固定reader](../../../jobs/OPERATIONS.md#jquants-native-operations)へ進みsummary/latestで代用しない。候補/省略/未生成/欠測と時点・Reading・保存範囲は[観測reference](../../../docs/reference/model-observations.md#judgment-use)に従う。

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

1. `stock-assessment-scaffold`へasofを指定し、[根拠とニュース](../../../docs/reference/stock-assessment.md#recent-news-research)を確認して執筆する。まず[統合した経済経路](../../../docs/reference/stock-assessment.md#analysis-domain-integration)を作り、Web提案の七観点にはそこへ効く支持・相反と不足を分解して記す。事業・成長・競争・懸念の前提を確かめるときは[外部見解との比較](../../../docs/reference/model-observations.md#public-research-reports)へ。PDF抽出失敗は原ページへ戻す。
2. `stock-assessment-evaluate <DRAFT>`で確認する。算術補助は`calculate`を使う。
3. `independent-review-scaffold`から別作業者が[独立検証](../../../docs/reference/stock-assessment.md#independent-second-pass)する。
4. `stock-assessment-evaluate <DRAFT> --review <REVIEW>`でpairを確認し、`stock-assessment-promote`で保存する。

scaffoldの`--from-research-id`は元日付/ID/hash付き素材コピー、`--from-assessment-id`はAssessmentの差分refresh。併用せず、コピーを評価済みとしない。

### 途中成果の保存と再訪

`company-research scaffold`はlatestまたは明示ID/hashからdraftを作る。調査範囲と追加/訂正/削除を編集し`company-research save`する。部分的理解のsnapshotとして検索不発も範囲/残る問いを保存でき、価値・価格・Reviewを捏造しない。

as_ofは情報境界で全内容の再検証日でない。元source/fact時刻を保ちコピーを再確認/削除の安全認定にしない。stale priorはlatestと統合して再保存し、同ID同内容は元保存時刻を返す。文字列参照の自動検証は保証しない。中断は理由とResearch ID/digestをrecord-unfinishedへ残し、部分保存をreviewed/完了としない。

## Trade Proposal

`investment-judgment status --workspace <WORKSPACE> --db <APP_DB>`とOperationを読む。評価保存とOperation束縛は別transactionで、再利用/束縛失敗だけなら`operation --db <APP_DB> record-assessment <OPERATION_ID> --assessment-id <ID>`で回復する。

未完対象だけを記録し、自動補充しない。

```bash
uv run baibai-engine operation --db <APP_DB> record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <REF>
```

通常の提案はcontextから作る。

```bash
uv run baibai-engine investment-judgment trade-proposal-context \
  --operation-id <OPERATION_ID> --db <APP_DB> --sqlite-path stores/market/market.sqlite
```

contextを読み、`trade-proposal-scaffold`へ同Operation/db・新proposal-id・outを渡す。固定全対象を[Trade Proposal](../../../docs/reference/trade-proposal.md)に沿って比較し、`trade-proposal-check <DRAFT> --db <APP_DB>`→別作業者の内容Review→`trade-proposal-publish <REVIEWED_DRAFT> --db <APP_DB>`へ進む。

全対象が理由/参照付きunfinishedで、公開済みProposalも束縛参照もない場合だけProposalを省ける。resultに確認範囲/評価未完、nextに未解決の問い/再調査条件を記す。reviewedや未束縛の公開済みProposalがあれば通常経路を使い、経済的見送り/保有判断をunfinishedにしない。0対象は選定理由を残し空Proposalなしで完了する。

## 判断の振り返り

旧判断と後日事実を照合する。企業知識の更新はResearch、現評価はrefreshへ分け、新Operation/Selection/Review/現在市場入力は不要。

```bash
uv run baibai-engine investment-judgment judgment-retrospective scaffold \
  --proposal-id <PROPOSAL_ID> --ticker <TICKER> --asof <ASOF> --db <APP_DB> --out <DRAFT>
uv run baibai-engine investment-judgment judgment-retrospective save <DRAFT> --db <APP_DB>
```

Assessment-onlyはproposal-id/tickerを`--assessment-id`へ替える。返されたexact原判断/Review/固定台帳を読み、節目理由・比較・lessonを書く。action/hash/eventを手転記せず、Assessment-onlyへ採否/注文を補わない。

元主張の対象/条件/期と後日観測をexact文書内のsource/factで結び、比較理由を残す。支持/反証はその主張に限り、提案全体の勝敗ではない。同名source/旧supportedを今回確認にせず、不足は理由付きunresolvedとする。注文/約定/Reviewはcontext/lessonへ記し、架空Research sourceを作らない。台帳未記録はunknown、遅延報告は新scaffold/新IDで照合する。

後日値上がり/公表は当時の価格条件/未公表を反証しない。利益があってもbasis誤りは改善対象、受容済み不利経路の損失なら手順不変も正常。損益と判断過程を分け、次回変える/変えない確認と理由を残す。

as_ofは元発行日以降のJST情報境界で保存時計ではない。後日URL取得は当時既知/bytes不変を保証しない。同ID同内容は元記録、続きは新ID、訂正はcorrects_refとlessonの理由を持つ新draft。`judgment-retrospective list/get`で有限に再訪し、単一latestで結論にせずlessonを現在fact/買付条件へ転記しない。

## 完了と再開

```bash
uv run baibai-engine operation --db <APP_DB> record-proposal <OPERATION_ID> --proposal-id <ID>
uv run baibai-engine operation --db <APP_DB> complete <OPERATION_ID> --result '<RESULT>' --next '<NEXT>'
uv run baibai-engine investment-judgment prepare \
  --operation-id <OPERATION_ID> --db <APP_DB> --workspace .cache/research/<ASOF> --force
```

record-proposalは公開済み提案の束縛。buy完了には具体termsを要し、sell terms不足の理由付き調査終了を具体提案完成としない。completedは有限作業終了で、評価/提案完成・採否・注文・約定とは限らない。正本は`operation --db <APP_DB> show <ID>`で読み、workspaceのincompleteと区別する。

Operationに保存された[Research Selection](../../../docs/architecture.md#store-authority)から再開し、selection-contextで選び直さない。forceは表示を再投影し執筆draftを保持する。DB commit後のfile失敗も返されたIDで再開する。別active workspaceを上書きせず、already_completedは戻さず、再依頼には新workspace/Operationを使う。取引/資金の報告は[Ledger](../ledger-record/SKILL.md)へ。

## 提案訂正・人間の非購入報告

[訂正と本人報告](../../../docs/reference/trade-proposal.md#提案の訂正と人間の報告)に従いexact Proposalを読む。

```bash
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> correct <CORRECTION_DRAFT> \
  --expected-latest-id <ID> --expected-latest-sha256 <SHA256>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> report <HUMAN_REPORT_DRAFT>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> show <PROPOSAL_ID>
```

競合は読直しblind retryしない。同ID同内容だけ冪等で、保存時計はエンジンが付ける。別記録を読戻す。
