# 保有・運用のWeb

台帳・保存成績・正式提案の履歴を表示し、未評価をゼロにしない。候補抽出・判断作成・台帳更新は[エンジン側](../docs/architecture.md#変更する場所)で行う。

## 表示の意味

[保存提案](../docs/reference/trade-proposal.md)と対応するAssessment/Reviewを表示し、現在価格・cash・注文資格は再計算しない。提案や成績が0件の場合と読取不能を区別し、一部の読取不能で他の表示を止めない。壊れた提案を別の提案で代用しない。

一覧は発行時刻をJSTの日付・午前/午後でまとめ、新しい発行から表示する。提案基準日・評価基準日・表示生成時刻とは別の時計である。レポートは不変proposal_idとtickerの組で選び、同じ銘柄の過去や複数銘柄の旧提案を消さない。会社名・業種は参照先のexact評価snapshotから取得し、欠損を現在のmasterで補わない。読取不能の提案件数と訂正済み原本を明示し、人間報告・提案全体の文脈も詳細に残す。最新1件のAPIは既存利用者向けに同じ選択規則で維持する。

提案は保存された要点と理由、七モデルの評価チャート、折りたたんだ詳細の順に読む。要点と理由は後半に重要留保や結論を含む場合があるため、文数で切らず全文を表示する。新規の短い要約は[提案作者](../docs/reference/trade-proposal.md#report-summary)が既存fieldへ記述し、Webは判断を再生成しない。

七モデルの観点はexact評価pairに保存された作者判断と出典だけを示す。未記録は0点/中立へ補完せず、支持の件数・総合点・L2取得状況を推定しない。根拠の対象日と取得時刻、評価基準日は別に表示する。

評価の見返りは同じpairの価格・期間・Base/Downsideを既存算術で換算し、保存前提と最も強い反対論を併記する。提案指値や現在価格へ差し替えず、旧表示データの未記録と評価未確定を分ける。

詳細には同じ評価の永久損失の検討と未解決の問いを表示する。資金・返済等の限定を七観点へ転記させず、判断・根拠確認の範囲・時点を原文のまま渡す。根拠の確認済みを安全保証、問いの存在を購入不可に変えない。旧表示データに含まれない場合を、リスクや問いがないことと区別する。

## 実装の入口

- [materialize](backend/src/baibai_web/materialize.py)：applicationとmarketから表示を生成する。生成と公開は別。
- [sources](backend/src/baibai_web/sources/)・[readmodel](backend/src/baibai_web/readmodel/)：正本の読取と表示への投影。提案の選択規則はエンジンの[query](../engine/src/baibai_engine/investment_judgment/trade_proposal/query.py)を使う。
- [contracts_export](backend/src/baibai_web/contracts_export.py)：Python表示modelからschema・TypeScriptを生成する。生成物を別仕様として手編集しない。
- [frontend](frontend/)・[edge](edge/)：表示と認証付き配信。全viewに既存の共通認証を適用する。

環境準備・Viteで確認できる範囲・buildは[development](../docs/development.md#webの確認)、本番への生成・公開・読戻しとWorker配備は[運用手順](../jobs/OPERATIONS.md#web-serving)に従う。
