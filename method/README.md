# Production methodology

productionで採用するGit管理のnormative methodologyを置きます。

| 項目 | 内容 |
| --- | --- |
| 所有 | macro reading、screening rules、research playbooksのdated revision |
| 所有しない | 表示設定、実行時状態、historical evidence |
| 入口 | CLIは持たず、engine loaderがactive revisionを読み取り専用で参照する |
| 依存境界 | codeをimportせず、Web表示、cloud topology、実行時dataを持ち込まない |
| 変更先 | 機械読み規則は`macro/reading`、screening閾値は`screening/rules`、research手順は`research/playbooks` |
| 正本・test | [doctrine](../docs/doctrine.md)、[domain reference](../docs/reference/README.md)、[tests/engine](../tests/engine)、[tests/contracts](../tests/contracts) |

methodはevidenceを確認した明示PRだけで変更し、reportから自動更新しません。実行時状態は[stores](../stores/README.md)、表示設定は[web/config](../web/config)、採否の証拠は[reports](../reports/README.md)が所有します。
