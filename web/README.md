# 保有・運用のWeb

資産・損益・保有、運用成績、タスク、最新の保存済み売買提案を表示する。台帳・保存成績・正式提案を正本とし、未評価をゼロへ置換しない。候補抽出や投資判断を行う画面ではない。

## 表示の意味

売買提案は訂正を反映した最新の保存本文と、その提案が参照する正確なAssessment/Reviewを表示する。当初の投資理由と今回の評価、原提案の行動と本人の非購入報告を分ける。現在の価格・cash・注文資格を再計算せず、対象取引日を実注文期限へ読み替えない。[提案・訂正・本人報告](../docs/reference/trade-proposal.md)の意味を保つ。

正常storeの提案0件と、store不在・破損による読取不能を区別し、最新提案が壊れていても古い正常提案へ戻さない。提案の不足は他の表示を止めない。選択・本文・参照評価は同じread-only transactionで読む。

## 実装の入口

- [materialize](backend/src/baibai_web/materialize.py)：applicationとmarketから4 viewを生成する入口。生成は公開ではない。
- [sources](backend/src/baibai_web/sources/)・[readmodel](backend/src/baibai_web/readmodel/)：正本の読取と表示への投影。提案の選択規則はエンジンの[query](../engine/src/baibai_engine/investment_judgment/trade_proposal/query.py)を使う。
- [contracts_export](backend/src/baibai_web/contracts_export.py)：Python表示modelからschema・TypeScriptを生成する。変更時はこの入口で更新し、生成物を別仕様として手編集しない。
- [frontend](frontend/)・[edge](edge/)：表示と認証付き配信。全viewに既存の共通認証を適用する。

ローカル表示はloopbackの `uv run baibai-web serve` とfrontendのViteを使う。環境準備・CIは[development](../docs/development.md)、本番への生成・公開・読戻しとWorker配備は[運用手順](../jobs/OPERATIONS.md#web-serving)に従う。表示の整理を既存objectやcredentialの削除と混同しない。
