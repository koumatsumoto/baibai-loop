# 保有・運用のWeb

資産・損益・保有銘柄、運用成績、タスク、最新の保存済み売買提案を表示する。台帳と保存成績・正式提案を正本とし、未評価をゼロへ置換しない。スクリーニング・マクロ・旧調査一覧・前回差分は表示しない。

売買提案は表示データ生成時点の全scopeから `(as_of, timezone付きpublished_atの瞬間, proposal_id)` が最大の1件を選び、保存順の本文とexact Assessment/Reviewの抜粋を表示する。当初の投資理由と今回の評価を分ける。正常storeで0件なら「保存済み提案なし」、最新の破損・store不在なら「読取不能」とし、古い正常提案へ戻さない。提案の不足・破損は他の3 viewを止めない。現在のmarket・cash・注文資格を再計算せず、対象取引日を実注文期限へ読み替えない。[行動と条件の意味](../docs/reference/trade-proposal.md)に従う。

`baibai-web serve` は既存どおりloopbackだけで起動する。配信Workerの既存認証を全viewに共通で適用し、提案専用のpassword・role・設定は増やさない。ローカル作業では `uv run baibai-web serve` とfrontendのViteを使う。品質確認は対象HEADのCIで行う。

Pythonの表示modelからcontracts/schemaとTypeScriptを生成する。変更時は `uv run python -m baibai_web.contracts_export` で更新する。表示はapplicationとmarketだけを読み、旧run・economic store・保存判断の全件走査を必要としない。提案の選択・本文・exact pairは同じread-only transactionで読む。

[運用手順](../jobs/OPERATIONS.md#web-serving)に従い、4 viewを先にR2へ公開・読戻ししてからUIを切り替える。新提案は `views/latest-proposal.json` へ保存し、既存viewの形とkeyは変えない。旧R2 objectやcredentialの削除はこの変更に含めない。
