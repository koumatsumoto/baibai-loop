---
name: investment-judgment
description: 人間の明示開始から有限対象を選び、Company Research・Stock Assessment・Independent Review・新規または保有の提案を完成する。
---

# Investment Judgment

## 開始と固定対象

既存Operationの続きは[完了と再開](#完了と再開)へ進み、新しいSelectionを作らない。旧形式の保存済みOperationもIDから再開する。旧draftや旧runを渡して新規開始しない。

通常の「売買提案して」は`discover_new`、候補外も含む銘柄指定は`specified_stocks`、保有見直しは`review_positions`。同じ依頼で選定から具体提案まで進め、全候補Triageや人間による事前銘柄選択は要求しない。newとholdingを混ぜず、追加買付依頼は[現行方針](../../../docs/doctrine.md#investment-strategy)と照合する。

```bash
uv run baibai-engine investment-judgment selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --db stores/application/baibai.sqlite --runs-path stores/screening/runs.sqlite
```

指定は`--scope specified_stocks --ticker <TICKER>`、保有は`--scope review_positions`を使う。ユーザーの目的から価値獲得の仮説を立て、五モデル・保有・既存調査を比較する。必要な候補外の対照も同じ固定generationから有限に読む。機械順位の極端値だけを順に選ばず、capacityを弱い対象で埋めない。選定は購入順位でなく、次に調べる価値の比較である。価値差と損失経路、答えによって採否・価格条件が変わる大きさ、調査で答えが得られる見込みを既存のselection_summary・rationale・research_questionへ記す。開示が豊富なだけで優先せず、同じ未解決の問いを繰り返すだけなら他の対象と比較する。

selection-contextは発行済みの候補政策と、そのgenerationのモデルを返す。未生成とpayload内の欠測を区別し、欠測を正常値へ補わない。prepareは選定した対象のモデルと政策本文を固定保存し、可変storeから再計算しない。

候補外や異なる価値獲得経路の少数の対照は、返された `screening_selection_id` を固定して同じcontextへ読める。`--compare-ticker` は `--ticker` と異なり、ユーザー指定銘柄・推薦集合・深掘り対象を変更しない。未生成のモデルは各社の `not_generated` に残る。実際の対象は比較後にdraftへ選び、候補外を毎回入れるノルマにはしない。

```bash
uv run baibai-engine investment-judgment selection-context \
  --request '<USER_START_REQUEST>' --scope discover_new \
  --screening-selection-id <SELECTION_ID> --compare-ticker <TICKER> \
  --db stores/application/baibai.sqlite --runs-path stores/screening/runs.sqlite
```

低PERなら利益の反復性、過去平均なら回復経路、資産proxyなら株主への帰属・解放、成長なら追加投資の見返りを比べる。候補抽出の名称をその企業の価値獲得経路と決めつけず、[経済性の問い](../../../docs/reference/stock-assessment.md#経済性から問いを選ぶ)へつなぐ。重要でも調べて確定しない未来は、調査の無期限延長でなく条件付き評価へ残す。

企業成長の範囲は保存モデルのrequested_start/endとcoverageで確認する。必要なら同じ固定入力から必要期間を再生成し、別generationとして明示する。prepare中に範囲を変えたり、最新原本へ付け替えたりしない。[期間・欠測と入力固定](../../../docs/reference/company-growth.md)に従う。

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

1. `investment-judgment stock-assessment-scaffold --workspace <WORKSPACE> --ticker <TICKER> --asof <YYYY-MM-DD>`で作る。返された`assessment_draft`を編集し、以下の`<DRAFT>`にそのpathを使う。保有は対象のholding子workspaceを使う。[Stock Assessment](../../../docs/reference/stock-assessment.md)から判断を変える経済的な問いを選び、一次資料へ戻る。
2. [条件付き価値](../../../docs/reference/stock-assessment.md#scenario-arithmetic)を組み立て、数値計算には`investment-judgment calculate <INPUTS>`を使える。PDFの抽出が空・崩れた場合は原ページへ戻り、記載なしやゼロとしない。
3. `investment-judgment stock-assessment-evaluate <DRAFT>`で確認する。`investment-judgment independent-review-scaffold --workspace <WORKSPACE> --ticker <TICKER>`からReviewを準備し、作者と別の作業者が原典・仮定・算術・反証を検討する。作者の入力や出力のコピーを独立検算としない。[不確実性の受容](../../../docs/reference/stock-assessment.md#uncertainty-and-decision)に従い、未知を一律保留にせず、受けるリスクと見返りの根拠を確認する。
4. `investment-judgment stock-assessment-evaluate <DRAFT> --review <REVIEW>`で一組を確認し、`investment-judgment stock-assessment-promote --workspace <WORKSPACE> --ticker <TICKER>`で保存する。core変更時はReviewを取り直す。数値未確定でも所見と理由を独立検証した評価は完成でき、調査自体の不足は未完了とする。

別日の当日算術は`stock-assessment-scaffold --from-assessment-id <ID>`から[差分refresh](../../../docs/reference/stock-assessment.md#assessment-refresh)を作り、新しいReviewを取る。

## Trade Proposal

statusとOperationを読み、保存評価は`operation record-assessment <OPERATION_ID> --assessment-id <ID>`で束縛する。未完了は理由と参照を記録し、対象を自動補充しない。

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

```bash
uv run baibai-engine investment-judgment trade-proposal-check <DRAFT> --db stores/application/baibai.sqlite
uv run baibai-engine investment-judgment trade-proposal-publish <REVIEWED_DRAFT> --db stores/application/baibai.sqlite
```

checkのvalidとreview_bindingを別に読み、作者と別の作業者が正規modelのdraft_sha256へ内容Reviewを束縛する。[Review・保存・preview](../../../docs/reference/trade-proposal.md#publication-review)に従い、入力事実が変わればsnapshotとReviewを取り直す。

## 完了と再開

公開済み提案を`operation record-proposal <OPERATION_ID> --proposal-id <ID>`へ記録し、現在payloadの固定selection/outcomesを保ってresult/nextを記し、`operation complete <OPERATION_ID> --payload <FILE>`へ渡す。buyを含む完了には具体termsが必要。sellのterms不足は理由付きで調査工程を終了できるが、価格・数量まで完成したと報告しない。人間の採否・broker操作・実約定を完了条件へ補完しない。

```bash
uv run baibai-engine investment-judgment prepare \
  --operation-id <OPERATION_ID> --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> --force
```

再開は保存入力から投影を復元し、元generation・旧runやmutable storeへ付け替えない。forceは生成投影を修復し、執筆済みdraftを保つ。commit後のfile失敗も返されたOperation IDから再開する。別activeを上書きせず、完了済みへ追記しない。同文の本当の再依頼は新workspaceと新Operationで扱う。

後続調査は現在の成果を確定して次Operationへ進む。実際に報告された注文・資金は[Ledger Record](../ledger-record/SKILL.md)、本番反映は[application反映](../../../jobs/OPERATIONS.md#application-db-を反映する)へ進む。monitoringは同じ問いの既存taskを確認して記録する。
