# Web

正本を読み取り専用で表示する。責務と依存は[architecture](../docs/architecture.md)、環境準備は[development](../docs/development.md)に従う。

`backend`はAPI・read model・materialization、`frontend`は画面、`edge`は認証付き配信、`config`は表示設定を所有する。domainの算術・保存条件はengineの`read_api`から使う。保存提案の評価参照と現在previewを区別し、後者で保存判断やbroker事実を変更しない。

## 開発と生成

local UIは`uv run baibai-web serve`で起動し、`http://127.0.0.1:8712/api/health`と画面を確認する。read modelの正本はbackend modelで、`contracts/read-model.schema.json`と`frontend/src/api/types.ts`を生成する。

```bash
uv run python -m baibai_web.contracts_export
```

modelを変えたら生成物を同じcommitへ含める。`contracts/routes.json`はmaterialized APIとserving keyの対応であり、全HTTP routeの台帳ではない。healthやlake gatewayは実装とedge testが所有する。deployとread model更新の順序は[運用](../jobs/OPERATIONS.md#workerをdeployする)に従う。

## 共有read

`https://<production-origin>/?share=<READ_ACCESS_TOKEN>`で通常UIを開く。tokenはsessionへ取り込んでURLから除き、owner passwordとは別に扱う。失効・再設定は[共有readの運用](../jobs/OPERATIONS.md#shared-read-setup)へ進む。

market lakeは同じ認証の`GET /api/lake/current`と`GET /api/lake/object?key=<key>`から原bytesを読める。[固定releaseの読取](../jobs/OPERATIONS.md#fixed-release-read)に従い、一分析内でcurrentを乗り換えない。Workerの接続はcanonical bucketのObject Read onlyとし、materializerや正本へのwrite入口を追加しない。
