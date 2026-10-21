# Production methodology

productionで採用するGit管理のnormative methodologyを置きます。

| 項目 | 内容 |
| --- | --- |
| 所有 | Reading、screening rules、research playbooksのdated revision |
| 所有しない | 表示設定、実行時状態、historical evidence |
| 入口 | CLIは持たず、engine loaderがactive revisionを読み取り専用で参照する |
| 依存境界 | codeをimportせず、Web表示、cloud topology、実行時dataを持ち込まない |
| 変更先 | Reading規則の元dated fileは`macro/reading`（保存判断の引用を保全し、別current copyを作らない）、screening閾値は`screening/rules`、research手順は`research/playbooks` |
| 正本・test | [doctrine](../docs/doctrine.md)、[domain reference](../docs/reference/README.md)、[tests/engine](../tests/engine)、[tests/contracts](../tests/contracts) |

methodは明示PRで採用し、reportから自動更新しません。dated revisionは上書きせず、対象loaderの契約に従って新しいrevisionを追加します。

dated revisionの内容と履歴はGitで管理し、同じ内容のchecksum台帳を別途維持しません。playbookのIDとApproach mappingは[playbooks README](./research/playbooks/README.md)を参照してください。

実行時状態は[stores](../stores/README.md)、表示設定は[web/config](../web/config)、採否の証拠は[reports](../reports/README.md)が所有します。
