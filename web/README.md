# 保有・運用のWeb

資産・損益・保有銘柄、運用成績、タスクを表示する。台帳と保存成績を正本とし、未評価をゼロへ置換しない。スクリーニング・マクロ・旧調査提案・前回差分は表示しない。最新売買提案レポートは[Issue #1615](https://github.com/koumatsumoto/baibai-loop/issues/1615)で別途設計する。

`baibai-web serve` は既存どおりloopbackだけで起動する。配信Workerの認証方式は変更しない。ローカル作業では `uv run baibai-web serve` とfrontendのViteを使う。品質確認は対象HEADのCIで行う。

Pythonの表示modelからcontracts/schemaとTypeScriptを生成する。変更時は `uv run python -m baibai_web.contracts_export` で更新する。表示はapplicationとmarketだけを読み、旧run・economic store・保存判断の全件走査を必要としない。

[運用手順](../jobs/OPERATIONS.md#web-serving)に従い、3 viewを先に公開・読戻ししてからUIを切り替える。旧R2 objectやcredentialの削除はこの変更に含めない。
