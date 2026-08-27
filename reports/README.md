# Historical evidence

methodology改善の事前登録、結果、外部操作記録、明示的なconsumer artifactを履歴として保存します。

| 項目 | 内容 |
| --- | --- |
| 所有 | `studies`のevidence、`operations`のbefore/probe/after、`published`の小さなmachine-readable surface |
| 所有しない | production methodology、実行時状態、現在の運用手順 |
| 入口 | CLIは持たない。consumerはschema、method identity、source studyを検証して`published`を読む |
| 依存境界 | reportからruntime codeをimportせず、runtimeからdated studyへ依存しない |
| 変更先 | 調査は`studies/YYYY-MM-DD-slug`、外部設定を伴う操作は`operations/YYYY-MM-DD-slug`、安定consumer artifactだけを`published`へ置く |
| 正本・test | [doctrine](../docs/doctrine.md)、[estimate calibration](../docs/reference/estimate-calibration.md)、[tests/web](../tests/web)、[tests/contracts](../tests/contracts) |

studyはstore/outputを観測しても正本を書きません。productionへの採用は別の明示PRで行います。dated evidenceは現在の文体へ遡及修正しません。
study固有のdataは各studyの`artifacts/`へ置きます。
現在の規範は[method](../method/README.md)、実行時状態は[stores](../stores/README.md)が所有します。
