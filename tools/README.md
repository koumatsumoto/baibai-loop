# Repository tools

開発・検証用toolを置きます。schema切替toolは必要なdeliveryの間だけ作成し、本番受入後に撤去します。runtime packageへは組み込みません。

| 項目 | 内容 |
| --- | --- |
| 所有 | quality gate、experiment、diagnostic、一回限りのschema切替 |
| 所有しない | production runtime、domain semantics、定期batch、実行時store |
| 入口 | stable public CLIは持たず、repository-local moduleまたはscriptを明示的に実行する |
| 依存境界 | runtime packageへの必要最小限の依存は許可する。engine、web、batchからtoolsへ依存しない |
| 変更先 | 品質検査は`quality`、改善計測は`experiments`、調査補助は`diagnostics`、一時的な切替は必要なときだけ`cutover` |
| 正本・test | [architecture](../docs/architecture.md)、[Python foundation](../docs/reference/python-foundation.md)、[tests/tools](../tests/tools)、[tests/contracts](../tests/contracts) |

開発・計測の生成物は明示したreportまたは検証コピーへ書きます。切替toolはsource version・保持条件を限定し、[運用手順](../batch/OPERATIONS.md#schema-cutoverを修正するとき)に従って検証コピーを準備します。本番反映は対応する明示手順で行います。experiment evidenceは[reports](../reports/README.md)、採用結果は明示PRで[method](../method/README.md)へ反映します。

開発環境の分担・準備・追加機能・クラウド環境への登録は
[Python foundation](../docs/reference/python-foundation.md#cloud-development-boundary)を参照してください。
