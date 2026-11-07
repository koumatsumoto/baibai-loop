# Development

責務と変更先は[architecture](architecture.md)、採否の基準は[doctrine](doctrine.md#development-investment-policy)に従う。

## 環境準備

Linux / Bash、bootstrap用Python 3.11以上、uvを用意する。実行版と依存の正本は`.python-version`・`pyproject.toml`・`uv.lock`。

```bash
bash tools/setup-dev.sh
source .cache/dev/env.sh
uv run baibai-engine --help
```

setupのPython/uv用pathは`.cache/dev/env.sh`で設定する。shellごとにsourceし、checkout移動後はsetupを再実行する。fixtureにsecret・実storeは不要。Windowsでの編集・依存導入とPOSIX運用scriptの検証は別である。

browserが必要な場合だけ追加する。

```bash
bash tools/setup-dev.sh --with-browser
```

Webを編集する場合だけ`.node-version`のNodeとnpmを用意し、`npm --prefix web/frontend ci`、`npm --prefix web/edge ci`で依存を導入する。

基本setupと追加downloadの成功を区別する。403をproxy解除・TLS無効化で迂回しない。

## 実行環境と接続

通常開発と本番操作の範囲、Actions/認可環境の使い分けとcredentialの配置は[OPERATIONS](../jobs/OPERATIONS.md#actions-write-boundary)に従う。

保存環境のInstall scriptは対象checkoutでsetupし、起動時はcheckout/AGENTS確認後にenv.shをsourceする。一時的な`/workspace`を登録先にせず、`.env`・OAuth state・秘密をimage snapshot/Start skillへ埋め込まない。

実データが必要な時だけ`.env.sample`と運用手順に従いread-only接続する。Personal Vaultは保存だけでtaskへ渡らず、対象環境のKey・Type・Applies toと要求keyを揃える。S3署名のraw credentialにNetwork secretのplaceholderは使えず、secret保存とnetwork allowlistも別設定。秘密値を出さず、設定の有無と実読取で確認する。

## 変更と検証

branchで変更し、修正後の対象HEADのCIを確認する。push前の完全local gateは必須にしない。検査commandと対象は[workflows](../.github/workflows/)・`pyproject.toml`・各package設定を正本とする。

domainの契約はownerのtest、CLI・Jobsは入出力と接続を検証する。同じvalidationを内部計算や全層へ重ねず、fixture共通化は意味が同じ場合に行う。

実store/provider受入は合成fixtureと分け、CIのskipを受入成功としない。R2 integration testは外部書込・削除を伴うため、明示依頼された専用空bucketとcredentialでのみ、共有bucketなら`-n 0`で逐次実行する。既存本番データの有限読取は別途、対象・保存先・書込範囲を明示する。

指標・取得・Reading method変更時の観測store確認は[運用](../jobs/OPERATIONS.md#指標観測とregistryの修正)へ。L2入力確認にL3全履歴の先行検査を要求せず、文書変更だけでlive取得・本番再発行をしない。

検証・提出の共通規則は[AGENTS](../AGENTS.md)に従い、通常変更に別の承認手順を追加しない。
