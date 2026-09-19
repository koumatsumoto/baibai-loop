# Web contracts

backendのread modelとUIの型を同じ正本から生成するための、check-inされた契約です。

| 項目 | 内容 |
| --- | --- |
| 所有 | `routes.json`のmaterialized view APIとserving JSONの対応、生成済み`read-model.schema.json` |
| 所有しない | domain model、UI独自の手書き型、`/api/health`のplain dict |
| 入口 | `web/backend/src/baibai_web/readmodel/models.py` → `web/contracts/read-model.schema.json` → `web/frontend/src/api/types.ts` |
| 依存境界 | backend modelが正本。local API、materialized JSON、UIは同じshapeを使う |
| 変更先 | route mappingは`routes.json`、read model変更はbackend modelを直してschemaとTypeScriptを再生成 |
| 正本・test | [web README](../README.md)、`tools/quality/drift/check_readmodel_contract.py`、`npm run build` |

`routes.json`はmaterializerの対応表であり、すべてのHTTP routeの台帳ではない。healthやraw L1 gatewayは対応する実装・edge testで確認する。

## 生成と確認

~~~bash
uv run python -m baibai_web.contracts_export
uv run python -m baibai_web.contracts_export --check
~~~

modelを変えたら生成物を同じcommitへ含め、`npm run build`でUI consumerを確認します。drift gateは生成忘れを拒否します。

schemaのpropertyはすべて`required`です。`model_dump_json`とFastAPIの`response_model`はfieldを省略しません。code deployとmaterializeの間だけ旧artifactが残り得るため、[Batch operations](../../batch/OPERATIONS.md)の順序とUIのgraceful degradationで扱います。
