# 保有・運用のWeb

台帳・保存成績・最新の正式提案を表示し、未評価をゼロにしない。候補抽出・判断作成・台帳更新は[エンジン側](../docs/architecture.md#変更する場所)で行う。

## 表示の意味

売買提案は訂正後の最新保存本文とexact Assessment/Reviewを表示する。当初理由/今回評価、原行動/本人の非購入報告を分け、[提案・訂正・本人報告](../docs/reference/trade-proposal.md)の意味を保つ。現在価格・cash・注文資格は再計算せず、対象取引日を実注文期限にしない。

正常storeの提案0件と、store不在・破損による読取不能を区別し、最新提案が壊れていても古い正常提案へ戻さない。提案の不足は他の表示を止めない。選択・本文・参照評価は同じread-only transactionで読む。

## 実装の入口

- [materialize](backend/src/baibai_web/materialize.py)：applicationとmarketから表示を生成する。生成と公開は別。
- [sources](backend/src/baibai_web/sources/)・[readmodel](backend/src/baibai_web/readmodel/)：正本の読取と表示への投影。提案の選択規則はエンジンの[query](../engine/src/baibai_engine/investment_judgment/trade_proposal/query.py)を使う。
- [contracts_export](backend/src/baibai_web/contracts_export.py)：Python表示modelからschema・TypeScriptを生成する。生成物を別仕様として手編集しない。
- [frontend](frontend/)・[edge](edge/)：表示と認証付き配信。全viewに既存の共通認証を適用する。

環境準備・frontendのみのVite起動/buildは[development](../docs/development.md#webの確認)に従う。ローカルAPIサーバーは持たず、ViteだけではDB由来の表示を確認できない。本番への生成・公開・読戻しとWorker配備は[運用手順](../jobs/OPERATIONS.md#web-serving)に従う。
