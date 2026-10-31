# Development

責務と変更先は[architecture](architecture.md)、採否の基準は[doctrine](doctrine.md#development-investment-policy)に従う。

## 環境準備

Linux / Bash、bootstrap用Python 3.11以上、uvを用意する。実行版と依存の正本は`.python-version`・`pyproject.toml`・`uv.lock`。

```bash
bash tools/setup-dev.sh
source .cache/dev/env.sh
uv run baibai-engine --help
```

setupは`.cache/dev/`へPython開発環境を用意する。shellごとにenv.shをsourceし、checkoutを移したらsetupを再実行する。secretや実storeがなくてもfixtureを使える。Windowsでの編集・依存導入はPOSIX運用scriptの動作確認とは別である。

browserが必要な場合だけ追加する。

```bash
bash tools/setup-dev.sh --with-browser
```

Webを編集する場合だけ`.node-version`のNodeとnpmを用意し、`npm --prefix web/frontend ci`、`npm --prefix web/edge ci`で依存を導入する。build・testは対象HEADのCIで確認する。

基本setupと追加downloadの成功を区別する。403をproxy解除・TLS無効化で迂回しない。

## 実行環境と接続

通常開発はコード・文書・検証コピーの編集とCI確認を行う。本番の定期取得・公開は既存Actionsを優先する。個別に依頼されたapplication更新は認可された運用環境から正規CLIと条件付き反映を使う。操作の範囲・credentialの配置は[OPERATIONS](../jobs/OPERATIONS.md#actions-write-boundary)が所有する。

保存環境のInstall scriptは対象checkoutでsetupを実行し、起動時はcheckoutとAGENTSを確認してenv.shをsourceする。一時的な`/workspace`のfileを登録先にしない。`.env`・OAuth state・秘密をimage snapshotやStart skillへ埋め込まない。

基本開発にsecretは不要。実データが必要な時だけ`.env.sample`と運用手順に従い、必要なread-only接続を使う。Personal Vaultの保存だけではtaskへ渡らず、対象環境のKey・Type・Applies toと要求するkeyを揃える。raw credentialが必要なS3署名にNetwork secretのplaceholderは代用できない。secretの保存はnetwork allowlistを設定しない。値を表示せず設定の有無と実際の読取で確認する。

通常のクラウド開発へwriter credentialを流用しない。追加課金禁止の依頼ではrunner・storage・外部API・R2を含む課金経路を確認し、環境接続を本番書込の許可と扱わない。

## 変更と検証

branchで変更し、PRの対象HEADのCIで確認する。push前の完全local gateは必須にしない。具体的な検査commandと対象は[workflows](../.github/workflows/)・`pyproject.toml`・各package設定を正本とし、ここへコピーしない。失敗を直したら新しいHEADで確認する。

domainの契約はownerのtest、CLI・Jobsは入出力と接続を検証する。同じvalidationを内部計算や全層へ重ねず、fixture共通化は意味が同じ場合に行う。

必要な実store・provider確認は合成fixtureと分ける。専用bucketを共有するR2受入は`-n 0`で逐次実行する。指標・取得・Reading methodを変えた場合の観測store確認は[運用](../jobs/OPERATIONS.md#指標観測とregistryの修正)へ進む。L2入力確認にL3全履歴の先行検査は要求しない。文書だけの変更でlive取得や本番再発行をしない。

R2ストレージのintegration testは外部への書込・削除を伴うため、明示依頼された専用の空bucketとcredentialがある場合だけ実行する。既存本番データを読む有限の受入とは別で、そちらも対象・保存先・書込範囲を明示して実施する。通常CIのskipを本番受入成功と扱わない。

検証・提出では実行済み、失敗、未実行と残る限界を示す。通常変更に別の承認手順・検査台帳・全domain再検証を追加しない。
