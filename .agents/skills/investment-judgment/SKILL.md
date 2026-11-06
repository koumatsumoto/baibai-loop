---
name: investment-judgment
description: 人間の明示開始から有限対象を選び、Company Research・Stock Assessment・Independent Review・新規または保有の提案を完成する。
---

# Investment Judgment

採否は[doctrine](../../../docs/doctrine.md)、原典・モデル統合・価値・独立検証は[Stock Assessment](../../../docs/reference/stock-assessment.md)、行動/指値/数量/内容Reviewは[Trade Proposal](../../../docs/reference/trade-proposal.md)に従う。ここは操作と再開の入口で、全optionは各commandの`--help`を参照する。

`<APP_DB>`は[application作業コピー](../../../jobs/OPERATIONS.md#application-db-を反映する)。DBと取得ETagを再開時も一組で保持する。machine pullはapplicationを含まない。

## 開始と固定対象

既存Operationは旧形式も[IDで再開](#完了と再開)する。新規依頼は選定から具体提案まで進め、旧draft/runで新Selectionを作らず、全候補Triage・人間の事前選定を要求しない。

```bash
uv run baibai-engine investment-judgment selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db <APP_DB> --runs-path stores/screening/runs.sqlite
```

銘柄指定は`--scope specified_stocks --ticker <TICKER>`（候補外可）、保有は`--scope review_positions`。new/holdingを混ぜず、[調査価値](../../../docs/reference/stock-assessment.md#経済性から問いを選ぶ)で選ぶ。候補外対照は返された`--screening-selection-id <ID> --compare-ticker <TICKER>`を同commandへ渡し、比較後に採る対象をdraftへ選ぶ。[候補入り・表示省略・未生成・欠測](../../../docs/reference/screening-runtime.md#candidate-discovery)を同一視しない。

再訪は`investment-judgment company-research catalog/get`と[振り返り](#判断の振り返り)を有限に読む。catalogの続きは返されたknown_atを固定しnext_after_tickerを使う。未review Researchと過去のreview済みpair、空とunreadableを分け、全履歴読取を前提にしない。

[Selection v2](../../../engine/src/baibai_engine/investment_judgment/workflow/selection.py)へcontextのidentity/判断日/cutoff/requestを引継ぎ、対象・問い・理由・代替・未確認範囲を書く。全社モデルを投入しない。必要な通常取得で不足を補い、単一対象の全期間backfillや欠測の経済的棄却はしない。新しい共通市場判断が必要なら[市場判断skill](../market-context-assessment/SKILL.md)を先に行う。

```bash
uv run baibai-engine investment-judgment prepare \
  --selection-file <SELECTION_JSON> --db <APP_DB> \
  --workspace .cache/research/<ASOF> --runs-path stores/screening/runs.sqlite
```

prepareが開始材料を保存しdomain-inputs.yamlへ投影する。後発根拠はAssessment sourcesでReviewし、Selectionを改変しない。完全結果は`operation --db <APP_DB> --format json show <ID>`のresearch_selection artifactのfull_result_refから読む。成長表示は`--company-growth-periods 24`または`all`で広げられるが[保存範囲を増やさない](../../../docs/reference/company-growth.md#investment-judgmentへの接続)。既存表示は再開のforceを使う。

native入力はanalysis_source.inputs、詳細は完全結果のInputSnapshot descriptorと[固定reader](../../../jobs/OPERATIONS.md#jquants-native-operations)を使い、summary/latestで代用しない。[時点](../../../docs/reference/data-sources.md)と[Reading](../../../docs/reference/market-context.md#reading)を保つ。

### 記事の原文へ戻る

Generation V2 shared_subjectsのcoverage/抽出状態/省略を読む。自動抽出はTSR医薬品卸/情報サービス調査とBOJ調節方針で、全ニュース抽出済みではない。重要原文だけprivate出力へ読む。

```bash
uv run baibai-engine articles exact-evidence \
  --release <RETAINED_LOOKUP_RELEASE> --manifest-sha256 <LOOKUP_MANIFEST_SHA256> \
  --reference-file <EXACT_DESCRIPTOR_JSON> --cache <PRIVATE_CACHE> --output-dir <NEW_PRIVATE_DIR>
```

探索は同じrelease/manifest/cacheの`articles list`へsource・受領UTC月・公表期間を渡し、private index.jsonから選ぶ。複数月は月ごと、続きは同条件のnext_after。公表日時不明は日時検索に入らない。原文を公開logへ転載せず、[本文範囲](../../../docs/reference/data-sources.md#public-articles)を保つ。

lookupと元lineageは別で、旧release全体の再読ではない。Research source.refへ原典URL、used_forへexact descriptorを残す。保存時の自動exact照合は保証されない。Assessment/Review/再開へ日付/限定も引継ぎ、再受領を独立票、旧feedを本日の変化、予定日を発生確認にしない。

## Company Research・Stock Assessment・Independent Review

同じworkspace/ticker/DBで次を行う（保有はholding子workspace）。引数は[CLI](../../../engine/src/baibai_engine/cli/investment_judgment.py)と各help、返されたfile pathを使う。

1. `stock-assessment-scaffold`へworkspace・ticker・asof・dbを渡し、返されたassessment_draftを執筆する。[ニュース](../../../docs/reference/stock-assessment.md#recent-news-research)も調べ、PDF抽出失敗は原ページへ戻す。
2. `stock-assessment-evaluate <DRAFT>`で確認する。`calculate`は算術補助で、原典確認の代わりではない。
3. `independent-review-scaffold`へ同じworkspace/ticker/dbを渡し、別作業者が原典・上流算式からReviewする。
4. `stock-assessment-evaluate <DRAFT> --review <REVIEW>`で一組を確認し、`stock-assessment-promote`へ同じworkspace/ticker/dbを渡して保存する。

scaffoldの`--from-research-id`は元日付/ID/hash付き素材コピー、`--from-assessment-id`は[refresh](../../../docs/reference/stock-assessment.md#assessment-refresh)。併用せず、コピーを評価済みへ昇格させない。

### 途中成果の保存と再訪

`investment-judgment company-research scaffold`でlatestまたは明示ID/hashからdraftを作り、調査範囲・追加/訂正/削除を編集し`company-research save`する。部分的理解のsnapshotであって差分メモではない。検索不発も範囲/残る問いで保存でき、価値・価格・Reviewを捏造しない。

as_ofは情報境界で全内容の再検証日ではない。元source/fact時刻を保ち、コピーを再確認や削除の安全認定にしない。stale priorはlatestを読み統合して再保存する。[同ID同内容](../../../engine/src/baibai_engine/investment_judgment/company_research/store.py)は元保存時刻を返す。部分保存をreviewed/Operation完了とせず、中断は理由とResearch ID/digestを次節のrecord-unfinishedへ残す。文字列参照の自動検証は保証しない。

## Trade Proposal

`investment-judgment status --workspace <WORKSPACE> --db <APP_DB>`とOperationを読む。評価保存とOperation束縛は別transaction。既存評価の再利用/束縛だけの失敗は`operation --db <APP_DB> record-assessment <OPERATION_ID> --assessment-id <ID>`で回復する。

未完対象だけに次を使い、自動補充しない。

```bash
uv run baibai-engine operation --db <APP_DB> record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <REF>
uv run baibai-engine investment-judgment trade-proposal-context \
  --operation-id <OPERATION_ID> --db <APP_DB> --sqlite-path stores/market/market.sqlite
```

contextを読み、`trade-proposal-scaffold`へ同Operation/db・新proposal-id・outを渡す。固定全対象を被覆して[指値/数量/待機](../../../docs/reference/trade-proposal.md#limit-selection)または[保有/回収](../../../docs/reference/trade-proposal.md#holding)を比較する。`trade-proposal-check <DRAFT> --db <APP_DB>`の正規draft_sha256へ別作業者の内容Reviewを束縛し、`trade-proposal-publish <REVIEWED_DRAFT> --db <APP_DB>`へ進む。validは内容Reviewでなく、変更時は[再snapshot/再Review](../../../docs/reference/trade-proposal.md#publication-review)する。

全対象が理由/参照付きunfinishedで、公開済みProposalも束縛参照もない場合だけProposal工程を省いて完了できる。resultへ確認範囲/評価未完、nextへ未解決の問い/再調査条件を記す。reviewedや公開済み未束縛Proposalがあれば通常経路を使い、経済的見送り/保有判断をunfinishedにしない。0対象は選定理由を残し空Proposalなしで終える。

## 判断の振り返り

旧判断と後日事実の照合/学びに使う。企業知識はResearch、現評価はrefreshへ分け、新Operation/Selection/Review/現在市場入力は不要。

```bash
uv run baibai-engine investment-judgment judgment-retrospective scaffold \
  --proposal-id <PROPOSAL_ID> --ticker <TICKER> --asof <ASOF> --db <APP_DB> --out <DRAFT>
uv run baibai-engine investment-judgment judgment-retrospective save <DRAFT> --db <APP_DB>
```

Assessment-onlyはproposal-id/tickerに代え`--assessment-id`。返されたexact原判断/Review/固定台帳を読み、節目理由・比較・lessonを書く。action/hash/eventを手転記せず、Assessment-onlyへ採否/注文を補わない。

元主張の対象/条件/期、後日観測、比較理由を分ける。支持/反証は選んだ主張の判定で提案全体の勝敗ではなく、[exact文書と文書内source/fact](../../../engine/src/baibai_engine/investment_judgment/judgment_retrospective/document.py)を結ぶ。同名sourceや旧supportedを今回確認と混ぜず、不足は理由付きunresolvedでよい。注文/約定/Review等はcontext/lessonへ記し、架空Research sourceを作らない。台帳未記録はunknown、遅延報告は新scaffold/新IDで照合する。

後日値上がり/公表は当時の価格条件/未公表を反証しない。利益が出てもbasis誤りは改善対象、受容済み不利経路の損失なら手順不変も正常。損益と判断過程を同じ採点にせず、次回変える/変えない確認と理由を残す。

as_ofは元発行日以降のJST情報境界で保存時計ではない。後日URL取得は当時既知/bytes不変を保証しない。同ID同内容は元記録、続きは新ID、訂正はcorrects_refとlessonの理由を持つ新draft。`judgment-retrospective list/get`で有限に再訪し、単一latestだけで結論にせずlessonを現在fact/買付条件へ転記しない。

## 完了と再開

```bash
uv run baibai-engine operation --db <APP_DB> record-proposal <OPERATION_ID> --proposal-id <ID>
uv run baibai-engine operation --db <APP_DB> complete <OPERATION_ID> --result '<RESULT>' --next '<NEXT>'
uv run baibai-engine investment-judgment prepare \
  --operation-id <OPERATION_ID> --db <APP_DB> --workspace .cache/research/<ASOF> --force
```

record-proposalは公開済み提案の束縛。buy完了には具体termsが必要で、sell terms不足の理由付き調査終了を具体提案完成と呼ばない。completedは有限作業終了で採否/注文/約定ではなく、評価/提案完成とも限らない。正本は`operation --db <APP_DB> show <ID>`で読み、workspaceのincompleteと区別する。

再開は保存入力から投影を復元し、元generation/旧run/latest storeへ付替えない。forceは執筆draftを保持する。DB commit後のfile失敗も返されたIDで再開し、別active workspaceを上書きしない。already_completedを戻さず、後日の再依頼は新workspace/Operationを使う。取引/資金は報告事実だけを[Ledger](../ledger-record/SKILL.md)へ、monitoringは同じ問いの既存taskを確認して記録する。認可されたapplication反映へ進む。

## 提案訂正・人間の非購入報告

[記録の意味](../../../docs/reference/trade-proposal.md#提案の訂正と人間の報告)に沿いexact Proposalを読む。

```bash
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> correct <CORRECTION_DRAFT> \
  --expected-latest-id <ID> --expected-latest-sha256 <SHA256>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> report <HUMAN_REPORT_DRAFT>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> show <PROPOSAL_ID>
```

競合は読直してblind retryしない。同ID同内容のみ冪等で保存時計はエンジンが付ける。原提案/Review/完了Operation/台帳の不変と別記録を読戻してapplicationへ反映する。
