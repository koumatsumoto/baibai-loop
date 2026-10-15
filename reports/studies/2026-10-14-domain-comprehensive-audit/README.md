# ドメイン整理の全追跡ファイル点検

## この記録の適用範囲

以下は初回作業担当による、記載した実装 head と当時の台帳の記録です。「全件確認」「未確認0件」「最終検証」は、その時点の作業範囲に限ります。親による独立した全ファイルの意味レビューや、その後の変更・本番切替の完了を示すものではありません。

後続の直接レビューで追加修正が見つかっています。後続の全ファイル・横断レビューと本番反映は [PR #1539](https://github.com/koumatsumoto/baibai-loop/pull/1539) と [切替記録](../../../batch/OPERATIONS.md#research-domain-cutover)を参照してください。以下の件数・テスト結果・実データ未検証の記述は、当時の結果として保持します。

起点: `9d4ca963d865908349726d2e468739ad733446ca`（PR #1536）。
この記録は今回の点検範囲・所見・検証の証拠であり、恒久的な監査機構ではない。

## 範囲と進行

起点の `git ls-files` 全1087件を [tracked-files.txt](./tracked-files.txt) に固定した。
コード・テスト・設定/CI・active文書/skills・運用工具・生成物・dated method・履歴資料を含む。
別担当のmixed Triage修正を含むbase `300824dcaf18f097fdb89c2f91607e0b72af020e` を統合済み。最初のレビュー用snapshotでは追跡1088件中1039件を確認し、49件を未確認として記録した。その後残る全件と追加ファイルを確認し、現在の追跡1094件すべてを台帳と照合した。
[file別coverage](./coverage.tsv) と [未確認一覧](./unconfirmed-files.txt) を参照。追加した監査記録とWeb pagination helper/testも台帳に含める。未確認は0件。起点1087件の一覧は起点git treeとの完全一致を確認し、変更後の全追跡pathをcoverageに含めた。

## 早期所見と整理方針

- Research selectionはscopeごとにnew/holdingを分けcapacityを保存する。新規/保有合算3対象という説明を訂正する。
- Webの旧CAA API導線、batch公開CAA→Thesisの確認手順、撤去済み自作MCPの現行説明を整理する。
- Ledger RecordはInvestment Proposal参照と提案なしの確認済み直接事実を分け、実CLIに合わせて作り直す。
- Research skillを操作順序の正本とし、referenceには意味と制約を残す。保有入口の重複を減らす。
- 企業評価のuncertain、未完了調査、Proposalのdeferを区別する。
- 旧payload/IDを保全する切替工具とdated evidenceは現行仕様に書き換えない。

`tools/cutover/domain_v1.py` は別担当のmixed Triage修正対象のため編集しない。
mainへの直接push、merge、本番切替、認証/権限変更は実施しない。

## 変更と検証境界

現行docs/skillsをResearch selection → Stock Assessment/Assessment Review → Investment Proposal → human broker fact → Ledgerへ統合。候補scopeとcapacity、直接事実、未評価/未完了/deferの区別を実CLIへ合わせた。

runtimeでは未使用status writer、旧cache引数、無効metadata吸収、旧backfill validator、producer不在のdaily休日skipを除去。共通の計算/ハッシュ処理とテストschemaを正本へ集約した。Webの空load_errors契約を除去し、提案履歴は全paginationを読む。Task tickerは正準4文字へ合わせた。

米国純流動性はFed資産/TGAが百万USD、reverse repoが十億USDなのに全項目を同倍率で換算していた。`(assets−tga)/1000−reverse_repo`へ修正し、8,000,000 / 500,000 / 1,000入力の結果を7,499から6,500十億USDへ訂正する回帰を追加。既存保存vintageを上書きせず、訂正版source provenanceを区別する。本番storeの再計算・切替は実施しない。local store検証は実行を試みたが `stores/market_context/indicators.sqlite` 不在で開けず、実データ検証は未実施。

履歴reportは本文・数値・hash・旧payloadを保全し、完了済みの状態表示と移動で壊れた相対リンクのみ修正。published calibration/保持schema2のSHAはacceptanceと一致し、現行型検証と全quintile/basis coverage集計も照合した。外部原本や非公開storeを再取得・再測定したものではない。

## 最終検証

実装head `33bbdd1473d6f5c30b9d9d7a5b01a02b51ec00c4` に対して実施。

- frozen sync、Ruff format/check、mypy（440 files）、import-linter（34 contracts）、全drift gate: 成功。
- Python全体: 4610 passed、8 skipped、coverage 87.44%。skipは専用資格情報を構成していないR2 acceptanceのみ。
- Frontend: npm ci / lint / build、313 tests成功。既存lint警告8件あり。
- Worker: npm ci / generated types check / typecheck、106 tests、deploy dry-run成功。
- Bandit成功。pip-auditはruntime/all-groupsとbuild-systemの両方で既知脆弱性なし。npm auditは両packageとも脆弱性0件。

初回gateで見つかったpagination型推論と旧query正規化を修正。削除済package directoryに残る追跡外bytecodeは確認して除去した。PythonにはSQLite connectionのResourceWarning等1171件、Banditには既存nosec注釈の警告が残る。gate失敗を成功として扱ってはいない。

実store検証はDB不在のため未実施。外部R2 acceptance、provider取得、OAuth smoke、本番公開は実行していない。
