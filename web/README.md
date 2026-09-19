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

materialized JSONとfrontend assetは生成しますが、application DBやdomain storeは書きません。store authorityは[architecture](../docs/architecture.md#store-authority)を参照してください。
表示分類は`config/macro-panel.yaml`、consumer契約を持つevidenceは`reports/published/`から読みます。serving R2 keyは`contracts/routes.json`が示す既存topologyを維持します。

## ローカルUI

Python・Node.jsの環境を準備し、repository rootから実行する。環境の前提は[Python foundation](../docs/reference/python-foundation.md)を参照する。

```bash
uv sync --frozen --all-groups
(cd web/frontend && npm ci && npm run build)
uv run baibai-web serve
```

各commandの成功後に次へ進み、`http://127.0.0.1:8712`を開く。local UIはstoreを更新しない。cloudへの反映は[batch運用](../batch/OPERATIONS.md)に従う。

## 共有read

`https://<production-origin>/?share=<READ_ACCESS_TOKEN>`で通常UIを開けます。frontendは最初のAPI
request前にtokenを専用sessionStorageへ取り込み、URLから`share`を除去します。session中は共有tokenを
Bearerに使い、401ではそのtokenだけを消して通常の認証画面へ戻ります。owner passwordのlocalStorageは
上書きしません。空または複数の`share`は保存せず、URLから除去します。

既存`/api/*`はownerの`VIEW_PASSWORD`または`READ_ACCESS_TOKEN`をBearerで受けます。Authorizationが
無い場合だけ`?share=<READ_ACCESS_TOKEN>`も受け、headerが不正でもqueryでは救済しません。
`VIEW_PASSWORD`はqueryで受けず、複数`share`は400です。共有secretが未設定・空でもowner accessは使えます。
APIはGET専用、no-storeです。share付きbootstrap documentはno-store / no-referrer /
noindex, nofollow, noarchiveを返し、通常のstatic配信は維持します。

market L1は同じ認証で`GET /api/lake/current`と`GET /api/lake/object?key=<object-key>`からraw bytesを
取得できます。canonical bucketへの接続はbucket-scoped Object Read only S3 credentialを使い、Workerへ
write-capableなcanonical R2 bindingを付けません。新routeはmaterializer outputではないため
`contracts/routes.json`へ追加せず、`edge/tests`でtransport境界を検証します。

fixed-release読取と完全性確認は[market lake](../docs/reference/market-lake.md#shared-raw-read)、
secret設定・rotation・撤回・利用側受入は[運用手順](../batch/OPERATIONS.md#shared-read-setup)を参照してください。
