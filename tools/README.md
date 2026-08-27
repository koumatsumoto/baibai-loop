# Developer tools

repositoryを開発・検証するための非production toolを置きます。

| 項目 | 内容 |
| --- | --- |
| 所有 | quality gate、experiment、generator、diagnostic |
| 所有しない | production runtime、domain semantics、定期運用、実行時store |
| 入口 | stable public CLIは持たず、repository-local moduleまたはscriptを明示的に実行する |
| 依存境界 | runtime packageへの必要最小限の依存は許可する。engine、web、batchからtoolsへ依存しない |
| 変更先 | CI・driftは`quality`、改善計測は`experiments`、assetは`generators`、調査補助は`diagnostics` |
| 正本・test | [architecture](../docs/architecture.md)、[Python foundation](../docs/reference/python-foundation.md)、[tests/tools](../tests/tools)、[tests/contracts](../tests/contracts) |

生成物は明示されたassetまたはreportだけへ書き、runtime storeの正本を更新しません。experiment evidenceは[reports](../reports/README.md)、採用結果は明示PRで[method](../method/README.md)へ反映します。
