---
name: investment-judgment
description: 人間の明示開始から有限対象を選び、Company Research・Stock Assessment・Independent Review・新規または保有の提案を完成する。
---

# Investment Judgment

## 開始と固定対象

既存Operationは旧形式もIDから[再開](#完了と再開)し、旧draft/runで新Selectionを作らない。クラウドへ反映する作業は[application作業コピー](../../../jobs/OPERATIONS.md#application-db-を反映する)のDBと取得ETagを揃え、再開でもその組を保持する。machine pullはapplicationを含まない。以下の`<APP_DB>`はその作業DBを指す。

通常の「売買提案して」は`discover_new`、銘柄指定は候補外も含め`specified_stocks --ticker <TICKER>`、保有見直しは`review_positions`。同じ依頼で選定から具体提案へ進み、全候補Triageや人間の事前選定を要求しない。new/holdingを混ぜず、追加買付は[投資戦略](../../../docs/doctrine.md#investment-strategy)と照合する。

```bash
uv run baibai-engine investment-judgment selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db <APP_DB> --runs-path stores/screening/runs.sqlite
```

[経済性の問い](../../../docs/reference/stock-assessment.md#経済性から問いを選ぶ)から、判断・価格条件を変える問いと答えを得られる見込みを比較する。これは購入順位ではなく調査価値の選定であり、機械順位・開示量でcapacityを埋めない。同じ未解決の問いの反復も他対象と比べる。[Screeningの引渡し](../../../docs/reference/screening-runtime.md#candidate-discovery)を読み、表示省略・未生成・payload内の欠測を区別する。候補入りは購入資格ではなく、順位低下も売却理由ではない。

候補外対照は同commandへ返された`--screening-selection-id <ID> --compare-ticker <TICKER>`を指定する。比較はユーザー指定・候補集合・深掘り対象を変えず、毎回のノルマでもない。対象は比較後にdraftへ選ぶ。

再訪時は関連する保存材料を有限に読む。Research/Assessment一覧は買付順位ではなく、`unreadable`は調査なしではない。振り返りのlessonは現在の事実・買付条件へ転記せず、今回の問いへ戻す。空一覧は正常で全社登録・全履歴読取を求めない。

```bash
uv run baibai-engine investment-judgment company-research catalog \
  --asof <ASOF> --known-at <INPUT_KNOWN_AT> --limit 20 --db <APP_DB>
uv run baibai-engine investment-judgment company-research get \
  --research-id <ID> --sha256 <DIGEST> --db <APP_DB>
uv run baibai-engine investment-judgment judgment-retrospective list \
  --ticker <TICKER> --limit 20 --db <APP_DB>
uv run baibai-engine investment-judgment judgment-retrospective get \
  --retrospective-id <ID> --sha256 <DIGEST> --db <APP_DB>
```

catalogの続きは返された`known_at`を固定し`--after-ticker <NEXT_AFTER_TICKER>`で読む。Assessment全文はgetのResearch ID/hashに代えて`--assessment-id <ID>`、振り返りは`has_more`なら必要に応じlimitを広げる。未review Researchと過去のreview済みAssessmentを区別し、気になる企業だけ候補外比較へ接続する。

新規draftは[GeneratedResearchSelectionDraft / SelectedTarget](../../../engine/src/baibai_engine/investment_judgment/workflow/selection.py)のschema_version=2を使う。contextのgeneration・screening_selection・判断日・cutoff・requestを引き継ぎ、対象と問い・理由・代替・未確認範囲を記す。全社モデルを一括投入しない。入力不足は経済的棄却と分け、固定generationとsource受領を確認して必要な通常取得で補う。取得不能は不足のまま残し、単一対象のために全期間backfillしない。共通市場判断を新作する場合は[市場判断skill](../market-context-assessment/SKILL.md)を先に完了する。

```bash
uv run baibai-engine investment-judgment prepare \
  --selection-file <SELECTION_JSON> --db <APP_DB> \
  --workspace .cache/research/<ASOF> --runs-path stores/screening/runs.sqlite
```

prepareは対象のモデル・政策・開始材料を保存し、`domain-inputs.yaml`へ投影する。後から読む根拠はAssessmentのsourcesへ加えてReviewし、元Selectionの開始入力へ挿入しない。[入力時点](../../../docs/reference/data-sources.md)と[Reading](../../../docs/reference/market-context.md#reading)を確認する。

完全結果は`operation --db <APP_DB> --format json show <ID>`の`payload.artifacts`内の`kind=research_selection`から`full_result_ref`へ辿る。企業成長の長期表示はprepareの`--company-growth-periods 24`または`all`を使う。[表示窓は生成範囲を広げない](../../../docs/reference/company-growth.md#investment-judgmentへの接続)。既存workspaceの表示変更は再開の`--force`で行う。

native L1の受領・不足は`analysis_source.inputs`、詳細は完全結果のInputSnapshot descriptorと[source別reader](../../../jobs/OPERATIONS.md#jquants-native-operations)で読む。保存前の`generation_input_ref`は固定generationを指す。summaryやlatestで保存入力を代用しない。

産業/社会動向は固定Generation V2の`shared_subjects`を読む。自動意味抽出はTSRの医薬品卸/情報サービス業績調査とBOJ調節方針で、他sourceの索引を全ニュース抽出済みとしない。coverage・抽出状態・省略数を確認し、重要な`exact_evidence`だけprivate JSONへ保存する。

```bash
uv run baibai-engine articles exact-evidence \
  --release <RETAINED_LOOKUP_RELEASE> --manifest-sha256 <LOOKUP_MANIFEST_SHA256> \
  --reference-file <EXACT_DESCRIPTOR_JSON> --cache <PRIVATE_CACHE> --output-dir <NEW_PRIVATE_DIR>
```

探索は同じrelease・manifest・cacheで`articles list --source bbc_news --source fed_news --source ecb_news --month <UTC_RECEIPT_MONTH> --published-since <ISO_TIMESTAMP_WITH_OFFSET> --published-before <ISO_TIMESTAMP_WITH_OFFSET> --output-dir <NEW_PRIVATE_DIR>`を使う。privateな`index.json`から重要な原典だけへ戻り、公開logへ転載しない。受領月と公表期間を分け、期間不明は日時検索に入らない。複数月は月ごと・同じ条件の`next_after`で有限に読む。同`article_version_id`の再受領を独立根拠、古いfeed記事を本日の変化にしない。[本文範囲の限界](../../../docs/reference/data-sources.md#public-articles)を保ち、企業利益・資本への接続はL3仮説として反証する。

lookupと元lineageを区別し、元release全体の再読としない。Researchの`source.ref`に原典URL、`used_for`にexact descriptorを残す。予定は発表時点の主張であり、予定日を発生済みfactの日付にしない。Research保存はdescriptorの自動exact照合を保証しない。Assessment・Review・再開でも参照・日付・限定を保つ。

## Company Research・Stock Assessment・Independent Review

1. `investment-judgment stock-assessment-scaffold --workspace <WORKSPACE> --ticker <TICKER> --asof <YYYY-MM-DD> --db <APP_DB>`を実行し、返された`assessment_draft`を編集する（保有はholding子workspace）。[直近ニュース](../../../docs/reference/stock-assessment.md#recent-news-research)と[モデル統合](../../../docs/reference/stock-assessment.md#analysis-domain-integration)に従い、重要な支持・相反・不足を個社の仮定へつなぐ。全シグナルの深掘りは要求しない。
2. [条件付き価値と計算](../../../docs/reference/stock-assessment.md#scenario-arithmetic)を作る。`investment-judgment calculate <INPUTS>`で算術を確認できる。PDF抽出が空・崩れたら原ページへ戻り、記載なし・ゼロとしない。
3. `investment-judgment stock-assessment-evaluate <DRAFT>`、続いて`investment-judgment independent-review-scaffold --workspace <WORKSPACE> --ticker <TICKER> --db <APP_DB>`でReviewを準備する。[独立検証](../../../docs/reference/stock-assessment.md#independent-second-pass)は作者と別の作業者が原典・仮定・上流算式から行い、作者の入力/出力のコピーで済ませない。[未知を一律保留にしない](../../../docs/reference/stock-assessment.md#uncertainty-and-decision)。
4. `investment-judgment stock-assessment-evaluate <DRAFT> --review <REVIEW>`で一組を確認し、`investment-judgment stock-assessment-promote --workspace <WORKSPACE> --ticker <TICKER> --db <APP_DB>`で保存する。core変更は再Review。数値未確定でも所見・理由を独立検証した評価は完成でき、調査不足は未完了とする。

scaffoldの`--from-research-id <ID>`は同社・判断時点以前の素材を元日付とID/hash付きでコピーする。評価済み結論への昇格ではなく、価値・結論・Reviewを別途作る。別日の当日算術は`--from-assessment-id <ID>`から[差分refresh](../../../docs/reference/stock-assessment.md#assessment-refresh)と新Reviewを作る。両転記経路は併用せず、元記録を変えない。

### 途中成果の保存と再訪

`company-research scaffold --ticker <TICKER> --asof <ASOF> --db <APP_DB> --out <DRAFT>`はlatestの企業理解をコピーする。特定の元記録は`--from-research-id <ID> --from-sha256 <DIGEST>`で選ぶ。今回の`investigated_scope`と追加・訂正・削除を反映し、`company-research save <DRAFT> --db <APP_DB>`へ渡す。検索不発も確認範囲と残る問いで保存でき、価値差・買付価格・Reviewを創作しない。

Researchは差分メモでなく部分的理解のsnapshot。`as_of`は情報境界で全内容の再確認日ではない。source/factの時計を保持し、引継supportedを本日の再検証にしない。コピー機能は削除の意味的な安全を認定しない。stale priorはlatestを読み著者が統合し、blind retryしない。[保存契約](../../../engine/src/baibai_engine/investment_judgment/company_research/store.py)に従い、同ID同内容は元の保存時刻を返す。

保存はOperation完了・reviewed outcomeではない。中断時は`record-unfinished`へ理由とResearch ID/digestを残せるが、文字列参照の自動検証は保証しない。

## Trade Proposal

statusとOperationを読む。promoteは評価保存後にOperationへ束縛する別transactionである。既存評価の再利用や束縛だけの失敗は`operation --db <APP_DB> record-assessment <OPERATION_ID> --assessment-id <ID>`で回復する。未完了は理由・参照を残し、対象を自動補充しない。

```bash
uv run baibai-engine operation --db <APP_DB> record-unfinished \
  <OPERATION_ID> --ticker <TICKER> --reason '<REASON>' --ref <SOURCE_OR_WORKSPACE_REF>
```

全固定対象が理由・参照付き`unfinished`で、同Operationに公開済みProposalも束縛参照もない場合だけ、context・draft・Reviewを省略して[完了](#完了と再開)へ進める。`result`は確認範囲と評価未完了、`next`は未解決の問い・再調査条件を記す。部分Researchは保ち、未調査も理由・参照を残す。経済的見送り・保有判断をunfinishedへ変えない。reviewedがあれば通常経路を使い、公開済み未束縛Proposalも省略しない。0対象では選定理由を残し、空Proposalを作らず終了する。

```bash
uv run baibai-engine investment-judgment trade-proposal-context \
  --operation-id <OPERATION_ID> --db <APP_DB> --sqlite-path stores/market/market.sqlite
uv run baibai-engine investment-judgment trade-proposal-scaffold \
  --operation-id <OPERATION_ID> --proposal-id investment-proposal-<UUID> --out <DRAFT> --db <APP_DB>
uv run baibai-engine investment-judgment trade-proposal-check <DRAFT> --db <APP_DB>
uv run baibai-engine investment-judgment trade-proposal-publish <REVIEWED_DRAFT> --db <APP_DB>
```

全固定対象を被覆し、新規は[指値・数量・待機案](../../../docs/reference/trade-proposal.md#limit-selection)、保有は[継続と回収](../../../docs/reference/trade-proposal.md#holding)を比較して理由・見直し条件を記す。checkのvalidは内容Reviewではない。[Review・保存](../../../docs/reference/trade-proposal.md#publication-review)に従い別の作業者が正規draft_sha256へ内容Reviewを束縛してからpublishする。入力事実が変わればsnapshotとReviewを取り直す。

## 判断の振り返り

旧判断と後日事実を照合し学びを残す場合に使う。企業知識の更新はResearch、現在の再評価はrefreshとし、同文を重複保存しない。新Operation・Selection・独立Review・現在市場入力は前提にしない。

```bash
uv run baibai-engine investment-judgment judgment-retrospective scaffold \
  --proposal-id <PROPOSAL_ID> --ticker <TICKER> --asof <ASOF> --db <APP_DB> --out <DRAFT>
uv run baibai-engine investment-judgment judgment-retrospective save <DRAFT> --db <APP_DB>
```

Assessment-onlyはproposal-id/tickerの代わりに`--assessment-id <ID>`。返された`context`のexact原判断・Review・固定台帳を読み、`draft`の節目・比較・lessonだけを書く。action/hash/台帳eventを手で転記せず、Assessment-onlyへ採否・注文を足さない。deferは経済的棄却ではない。

- 節目の理由を記し、元主張の対象・条件・期間を保持する。`original_claim`・後日観測`observed`・比較理由`reason`を分け、支持/反証/未解決を選んだ主張について判定する。提案全体の勝敗ではない。
- supported/refutedはexact Research/Assessmentと文書内source（必要ならfact）を[引用契約](../../../engine/src/baibai_engine/investment_judgment/judgment_retrospective/document.py)に沿って結ぶ。同名source IDは文書ごとに区別し、既存supportedを今回の確認へコピーしない。根拠不足は理由付きunresolvedだけでも保存できる。
- 注文・約定・Review完成等はcontextとlessonへ記し、架空のResearch sourceにしない。意味のあるlessonだけでもよい。台帳未記録はunknownで、未約定と推測しない。遅延報告は旧記録へ後付けせず、新scaffold・新IDで照合する。
- lessonには次回変える/変えない確認と理由を残す。後日の値上がりは当時の「1,100円ではPmax 1,000円超過」を反証せず、後日公表は当時の未公表を誤りにしない。後知恵と当時の見落としを分ける。利益が出ても株数basisの誤りは改善対象だが、引き受けた不利経路の損失だけなら手順変更なしも正常。実現損益と判断過程を同じ採点にしない。

`as_of`は元発行日以降のJST情報境界で、保存時計ではない。後日取得は当時既知やURL原本bytes不変を保証しない。同ID同内容は元記録を返し、続きは新ID、訂正は新draftの`corrects_ref`に旧ID/digestとlessonの訂正理由を残す。一覧はorigin・節目・保存日・訂正先で読み、単一latestだけを結論にしない。

## 完了と再開

通常経路は`operation --db <APP_DB> record-proposal <OPERATION_ID> --proposal-id <ID>`で公開済み提案を明示的に束縛し、各経路とも`operation --db <APP_DB> complete <OPERATION_ID> --result '<RESULT>' --next '<NEXT>'`で終える。固定入力・成果・参照は保持され、Proposalがあれば現在の成果とのscopeを照合する。buy完了は具体termsが必要。sellのterms不足でも理由付きで調査を終えられるが、具体提案完成と報告しない。採否・注文・約定を補完しない。

`completed`は有限作業の終了で、評価・提案完成とは限らない。正本は`operation --db <APP_DB> show <OPERATION_ID>`で読み、workspaceの評価`incomplete`と区別する。

```bash
uv run baibai-engine investment-judgment prepare \
  --operation-id <OPERATION_ID> --db <APP_DB> --workspace .cache/research/<ASOF> --force
```

再開は保存入力から生成投影を復元し、元generation・旧run・mutable storeへ付け替えない。forceは執筆draftを保つ。DB commit後のfile失敗も返されたOperation IDで再開する。別active workspaceを上書きせず、完了済みへ追記しない。`already_completed`はforceでもactiveへ戻らない。後日の再調査や同文の本当の再依頼は新workspace・新Operationへ進み、必要な保存Researchを読む。

報告された取引・資金だけを[Ledger](../ledger-record/SKILL.md)へ記録し、認可された[application反映](../../../jobs/OPERATIONS.md#application-db-を反映する)を行う。monitoringは同じ問いの既存taskを確認して記録する。

## 提案訂正・人間の非購入報告

[訂正・人間報告の意味](../../../docs/reference/trade-proposal.md#提案の訂正と人間の報告)に従いexact Proposalを読む。非購入を新no_actionへ置換しない。本人の言葉と報告/対象時点を分け、不明な注文結果を補完しない。

```bash
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> correct <CORRECTION_DRAFT> \
  --expected-latest-id <ID> --expected-latest-sha256 <SHA256>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> report <HUMAN_REPORT_DRAFT>
uv run baibai-engine investment-judgment proposal-records --db <APP_DB> show <PROPOSAL_ID>
```

expected latest/hashの競合は読直し、blind retryしない。同ID同内容のみ冪等で保存時計はエンジンが付ける。原提案・Review・完了Operation・台帳の不変と別記録をローカル読戻しで確認後、認可されたapplication反映へ進む。
