# 保有・運用のWeb

台帳・保存成績・最新の正式提案を表示し、未評価をゼロにしない。候補抽出・判断作成・台帳更新は[エンジン側](../docs/architecture.md#変更する場所)で行う。

## 表示の意味

[最新の保存提案](../docs/reference/trade-proposal.md)と対応するAssessment/Reviewを表示し、現在価格・cash・注文資格は再計算しない。提案や成績が0件の場合と読取不能を区別し、一部の読取不能で他の表示を止めない。壊れた最新提案を古い提案で代用しない。

## 実装の入口

- [materialize](backend/src/baibai_web/materialize.py)：applicationとmarketから表示を生成する。生成と公開は別。
- [sources](backend/src/baibai_web/sources/)・[readmodel](backend/src/baibai_web/readmodel/)：正本の読取と表示への投影。提案の選択規則はエンジンの[query](../engine/src/baibai_engine/investment_judgment/trade_proposal/query.py)を使う。
- [contracts_export](backend/src/baibai_web/contracts_export.py)：Python表示modelからschema・TypeScriptを生成する。生成物を別仕様として手編集しない。
- [frontend](frontend/)・[edge](edge/)：表示と認証付き配信。全viewに既存の共通認証を適用する。

環境準備・Viteで確認できる範囲・buildは[development](../docs/development.md#webの確認)、本番への生成・公開・読戻しとWorker配備は[運用手順](../jobs/OPERATIONS.md#web-serving)に従う。
