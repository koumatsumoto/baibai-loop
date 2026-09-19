# Macro store

`stores/macro/macro.sqlite`に観測・vintage・取得情報を保持します。Macro Contextはapplication DBへ保存します。

観測と改定の意味は[macro reference](../../docs/reference/macro.md)、正本と保持の境界は[architecture](../../docs/architecture.md#store-authority)、転送・復旧は[batch運用](../../batch/OPERATIONS.md)に従います。
