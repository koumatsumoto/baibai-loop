# Development

責務と変更先は[architecture](architecture.md)、採否の基準は[doctrine](doctrine.md#development-investment-policy)に従う。

## 環境準備

Linux / Bash、bootstrap用Python 3.11以上、uvを用意する。実行版と依存の正本は`.python-version`・`pyproject.toml`・`uv.lock`。

```bash
bash tools/setup-dev.sh
source .cache/dev/env.sh
uv run baibai-engine --help
```

shellごとにenv.shをsourceし、checkout移動後はsetupを再実行する。fixtureにsecret・実storeは不要。Windows側だけの作業ではPOSIX運用scriptを検証できない。

browserが必要なら`--with-browser`を追加する。Web編集時は`.node-version`のNode/npmで`npm --prefix web/frontend ci`と`npm --prefix web/edge ci`も実行する。

### Webの確認

`npm --prefix web/frontend run dev`はfrontend資産だけを配信する。ローカルAPI・SQLite読取・API proxyはなく、DB由来のDashboard・成績・保存提案は確認できない。型とbuildは`npm --prefix web/frontend run build`で確認する。

表示JSONの生成は`baibai_web.materialize`、認証付きAPIと画面の受入は[運用手順](../jobs/OPERATIONS.md#web-serving)に従い、fixtureやbuild成功と本番確認を分ける。

## 実行環境と接続

本番操作と認証は[OPERATIONS](../jobs/OPERATIONS.md#actions-write-boundary)に従う。実データが必要な場合は`.env.sample`も参照し、秘密をコードや共有成果物へ含めない。S3署名には実credentialが必要で、Network secretのplaceholderは使えない。

## 変更と検証

[AGENTS](../AGENTS.md)に従いbranchで変更し、対象HEADのCIを確認する。検証コマンドは[workflows](../.github/workflows/)・`pyproject.toml`・各package設定が正本。domain契約はowner、CLI/Jobsは入出力と接続をtestする。

実store/provider受入は合成fixtureと分け、CIのskipを受入成功としない。R2 integration testは書込・削除を伴うため、明示依頼された専用空bucketでのみ実行し、共有bucketなら`-n 0`で逐次実行する。

指標・取得・Reading method変更時の観測store確認は[運用](../jobs/OPERATIONS.md#指標観測とregistryの修正)へ。
