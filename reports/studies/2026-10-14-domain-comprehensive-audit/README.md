# ドメイン整理の全追跡ファイル点検

起点: `9d4ca963d865908349726d2e468739ad733446ca`（PR #1536）。
この記録は今回の点検範囲・所見・検証の証拠であり、恒久的な監査機構ではない。

## 範囲と進行

起点の `git ls-files` 全1087件を [tracked-files.txt](./tracked-files.txt) に固定した。
コード・テスト・設定/CI・active文書/skills・運用工具・生成物・dated method・履歴資料を含む。
別担当のmixed Triage修正を含むbase `300824dcaf18f097fdb89c2f91607e0b72af020e` を統合済み。追加fixtureを含む追跡1088件のうち、最初のレビュー用snapshotでは1039件を確認、49件は未確認として残す。
[file別coverage](./coverage.tsv) と [未確認一覧](./unconfirmed-files.txt) を参照。追加した監査記録とWeb pagination helper/testも台帳に含める。現在は点検中であり、全件確認済みとは扱わない。

## 早期所見と整理方針

- Research selectionはscopeごとにnew/holdingを分けcapacityを保存する。新規/保有合算3対象という説明を訂正する。
- Webの旧CAA API導線、batch公開CAA→Thesisの確認手順、撤去済み自作MCPの現行説明を整理する。
- Ledger RecordはInvestment Proposal参照と提案なしの確認済み直接事実を分け、実CLIに合わせて作り直す。
- Research skillを操作順序の正本とし、referenceには意味と制約を残す。保有入口の重複を減らす。
- 企業評価のuncertain、未完了調査、Proposalのdeferを区別する。
- 旧payload/IDを保全する切替工具とdated evidenceは現行仕様に書き換えない。

`tools/cutover/domain_v1.py` は別担当のmixed Triage修正対象のため編集しない。
mainへの直接push、merge、本番切替、認証/権限変更は実施しない。

## 中間snapshotの変更と検証境界

現行docs/skillsをResearch selection → Stock Assessment/Assessment Review → Investment Proposal → human broker fact → Ledgerへ統合。候補scopeとcapacity、直接事実、未評価/未完了/deferの区別を実CLIへ合わせた。

runtimeでは未使用status writer、旧cache引数、無効metadata吸収、旧backfill validator、producer不在のdaily休日skipを除去。共通の計算/ハッシュ処理とテストschemaを正本へ集約した。Webの空load_errors契約を除去し、提案履歴は全paginationを読む。Task tickerは正準4文字へ合わせた。

米国純流動性はFed資産/TGAが百万USD、reverse repoが十億USDなのに全項目を同倍率で換算していた。`(assets−tga)/1000−reverse_repo`へ修正し、8,000,000 / 500,000 / 1,000入力の結果を7,499から6,500十億USDへ訂正する回帰を追加。既存保存vintageを上書きせず、訂正版source provenanceを区別する。本番storeの再計算・切替は実施しない。local store検証は実行を試みたが `stores/market_context/indicators.sqlite` 不在で開けず、実データ検証は未実施。

履歴reportは本文・数値・hash・旧payloadを保全し、完了済みの状態表示と移動で壊れた相対リンクのみ修正。published calibration/保持schema2のSHAはacceptanceと一致し、現行型検証と全quintile/basis coverage集計も照合した。外部原本や非公開storeを再取得・再測定したものではない。

領域別fixture testsは成功。push前の完全local gateを次に実行し、結果は追記する。外部R2 acceptance、provider取得、OAuth smoke、本番公開は実行しない。
