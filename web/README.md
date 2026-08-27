# Web

正本を読み取り専用で表示するbrowser-facing presentation systemです。

| 項目 | 内容 |
| --- | --- |
| 所有 | `backend`のFastAPI/read model/materialization、`frontend`のReact、`edge`の認証付き配信、`config`の表示設定、`contracts`のroute/key inventory |
| 所有しない | domain logic、正本への書き込み、定期orchestration |
| 入口 | local UIは`baibai-web serve`、cloud配信は`web/edge`、materializerは`python -m baibai_web.materialize` |
| 依存境界 | Webはengineの`baibai_engine.read_api`だけを使う。batch、tools、engine internal writerへ依存しない |
| 変更先 | API/read model/materializerは`backend`、画面は`frontend`、Bearer/HSTS/R2 mappingは`edge`、表示設定は`config`、route contractは`contracts` |
| 正本・test | [architecture](../docs/architecture.md)、[Python foundation](../docs/reference/python-foundation.md)、[tests/web](../tests/web)、[tests/contracts](../tests/contracts)、frontend / edge各directoryの`tests` |

materialized JSONとfrontend assetは生成しますが、application DBやdomain storeは書きません。store authorityは[stores](../stores/README.md)を参照してください。
表示分類は`config/macro-panel.yaml`、consumer契約を持つevidenceは`reports/published/`から読みます。R2 keyは`contracts/routes.json`が示す既存topologyを維持します。
