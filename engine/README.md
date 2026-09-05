# Engine

Baibai Loopのdomain処理と正本への書き込みを所有します。

| 項目 | 内容 |
| --- | --- |
| 所有 | screening、macro、research、position、operation、task、application DB、query-only `read_api` |
| 所有しない | Web表示、定期実行、store転送、開発tool |
| 入口 | `baibai-engine`、`baibai_engine.read_api`、batch向け`baibai_engine.batch_api` |
| 依存境界 | `baibai_web`、`baibai_batch`、`tools`をimportしない。import-linterが検査する |
| 変更先 | domain処理・writer・CLIはengine、query-only入力は`read_api`、定期順序・retryはbatch、表示はweb |
| 正本・test | [doctrine](../docs/doctrine.md)、[domain language](../docs/domain-language.md)、[architecture](../docs/architecture.md)、[domain reference](../docs/reference/README.md)、[tests/engine](../tests/engine)、[tests/contracts](../tests/contracts) |

methodと各storeを読み、application DBとdomain-owned machine storeへwrite-time validationを通して書きます。Webからの書き込みは受け付けません。
repository pathの定義は`foundation.repository_layout`が所有します。
production ruleは[method](../method/README.md)、store authorityは[stores](../stores/README.md)、historical evidenceは[reports](../reports/README.md)が所有します。
