# 開発と検証

## 手元で始める

### 1. WSL / Linux で作業コピーを作る

Windowsでは PowerShell で `wsl -l -v` を実行し、Ubuntu が WSL 2 で動いていることを確認する。未導入なら `wsl --install -d Ubuntu` を実行し、再起動を求められたら再起動する。以降は **Ubuntu の Bash** で実行し、作業コピーは `~/baibai-loop` に置く（`/mnt/c` ではない）。Ubuntu 24.04 なら `sudo apt install git python3 curl` で基本ツールを用意できる。別途 [uv](https://docs.astral.sh/uv/getting-started/installation/) を入れておく。

初回は `main` の先端だけを取得する。既に作業コピーがあればクローンは不要。

```bash
cd ~
git clone --depth=1 --single-branch --branch main https://github.com/koumatsumoto/baibai-loop.git
cd baibai-loop
```

過去の差分を調べるときだけ、履歴の浅い作業コピーで `git fetch --deepen=50 origin main` を実行する。`main` の全履歴が必要なら `git fetch --unshallow origin` を使う。既存の作業コピーを作り直す必要はない。

### 2. 鍵なしで起動を確認する

Ubuntu/Linux の Bash、bootstrap用Python 3.11以上、uvを使い、リポジトリのルートで実行する。利用版は[.python-version](../.python-version)・[pyproject.toml](../pyproject.toml)、依存は[uv.lock](../uv.lock)で固定する。

```bash
bash tools/setup-dev.sh
source .cache/dev/env.sh
uv run baibai-engine --help
uv run baibai-jobs --help
```

shellを開き直したらenv.shをsourceする。checkoutを移動したらsetupをやり直す。通常のfixture・CLI helpに.envや本番storeは不要。ブラウザの検証が必要なときだけsetupに `--with-browser` を付ける。

### 3. 実データが必要なときだけ `.env` を用意する

[設定例](../.env.sample)から必要な項目だけを選ぶ。R2の読取には読み取り専用の `R2_ACCESS_KEY_ID`・`R2_SECRET_ACCESS_KEY` と `R2_ACCOUNT_ID`。日本株を直接取得するなら `JQUANTS_API_KEY`、米国相場を直接取得するなら Alpaca の `APCA_API_KEY_ID`・`APCA_API_SECRET_KEY` を追加する。EDINETとe-Statの鍵は各APIまで使うときのみ。GitHub Actions・Cloudflareの管理用トークン、npm公開トークン、本番Webのパスワードは共有用の設定に混ぜない。AlpacaのIEX取得可否とSIPの契約・権限は別。

**envshareを使う場合:** リポジトリのルートに配布元が用意した `envshare.enc` があるときだけ同期する。まだ配布されていなければこの手順は飛ばし、必要なら `.env.sample` を元に自分の `.env` を作る。既存の `.env` は上書きコピーしない。

[envshare](https://github.com/koumatsumoto/envshare#インストール) またはそのソースCLIを使う。このリポジトリのWeb用Node固定版がenvshareの要求未満なら、envshare用に別のNodeを一時的に使う。たとえば fnm では `fnm install 26` の後に次を実行する。

```bash
fnm exec --using=26 npm install --global @koumatsumoto/envshare
(
  set +x
  read -rs -p 'envshare key: ' ENVSHARE_KEY && printf '\n' &&
    ENVSHARE_KEY="$ENVSHARE_KEY" fnm exec --using=26 envshare sync --bootstrap
)
```

鍵は別経路で受け取り、コマンドの引数や履歴に直接書かない。すでに `.env` に鍵がある場合は `fnm exec --using=26 envshare sync` だけ。同期で `.env` の既存の無関係な設定は消えない。Gitには必要な場合だけ暗号文を置き、鍵・`.env`・平文の配布元は追加しない。CLIの単体テストには同期不要。

本番データを読む場合だけ、[運用手順の実行前確認](../jobs/OPERATIONS.md#actions-write-boundary)へ。提案のAppデータだけなら [`pull-app`](../jobs/OPERATIONS.md#application-db-を反映する) を使う（AWS CLIと空の作業領域が必要）。`pull-app` は必要なR2変数が未設定なら `.env` をBashで読むため、信頼できる設定だけを使い、R2の各行は設定例どおりの単純な `名前=値` にする。セットアップ確認のために全DBをダウンロードしたり、`publish.sh` や本番バッチを実行したりしない。

## 変更を検証する

[責務と変更先](architecture.md#変更する場所)を確認し、変更したdomainと、そのdomainを呼ぶCLI/Jobsのテストを選ぶ。例は一時DBを使うローカル検証。

```bash
uv run pytest -n 0 tests/engine/primitives
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
```

容量不足で止まったら `df -h . "${TMPDIR:-/tmp}"` で、作業コピーとテストの一時領域を別々に確認する。必要なら空きのある領域にその実行専用の一時ディレクトリを用意して `TMPDIR` を指定し、対象テストだけを同じ設定でやり直す。一時fixtureの保持・整理はpytestの既定に任せ、既存の作業データがあるディレクトリを `--basetemp` に指定しない。容量不足で中断した実行は検証未完了。依存・CIの条件は変えず、本番データも取り直さない。

よく変更する境界は、まず次の合成データのテストで確認できる。

| 変更した契約 | 最初のテスト |
| --- | --- |
| 受領の読戻しとseal | [JPXの原本・投影を読戻してからsealする](../tests/jobs/publication/test_jpx_verification.py) |
| 探索索引の問いとページ送り | [入力不足・分類・固定した個社比較を区別する](../tests/engine/investment_judgment/workflow/test_discovery_questions.py) |
| 提案の指値・価格・数量 | [観測価格と指値を分け、未確定の根拠で数量を作らない](../tests/engine/investment_judgment/trade_proposal/test_conditional_buy_terms.py) |

外部入力の採用を変えるときは、正常例に、期間・型・対象範囲の一つだけ違う行を混ぜる。行順が選択規則でない場合は、並びを逆にしても採用値と採用理由が同じかを試す。行順が変わった原本のハッシュまで同じである必要はない。受領を扱う場合は、同じ受領の再読、同じ内容の別受領、同じIDで内容が矛盾する受領を分け、締め時刻の前後で使える原本を確認する。不具合修正では旧版で失敗する反例を残し、別担当が最終差分で再実行する。正当なゼロ・欠測・対象外の資料・版を固定した過去結果を、新しい入力規則で壊さない。

PR全体の検査は[Python CI](../.github/workflows/ci.yml)と[Web CI](../.github/workflows/web.yml)が正本。通常のPythonテストは[fixture](../tests/conftest.py)で実通信を止め、公開や配備を検証しない。[R2実通信の受入テスト](../tests/integration/test_lake_r2_acceptance.py)は通常スキップする。明示依頼された時だけ、書込・削除が許可された専用の非本番bucketで `-n 0` を使う。ケースごとの固有prefixで作成を確認できたkeyだけを掃除する。書込結果が不明なら実行ログのprefixを確認する。mock・CI・実データ受入の結果を混同しない。

<a id="webの確認"></a>
## Webの確認

[.node-version](../.node-version)のNodeと対応するnpmを使う。UIの型検査・ビルドは表示テストを実行しないので、表示を変えたら両方を確認する。

```bash
npm --prefix web/frontend ci
npm --prefix web/frontend run build
npm --prefix web/frontend test
```

Workerや共通の配信契約を変えたら、上のUIビルドに続いてWorkerの生成型、型検査、ルートの動作を確認する。次のコマンドは配備しない。

```bash
npm --prefix web/edge ci
npm --prefix web/edge run types:check
npm --prefix web/edge run typecheck
npm --prefix web/edge test
```

`npm --prefix web/frontend run dev` は画面資産のみを配信し、ローカルAPIやSQLiteには接続しない。表示データの生成・認証付きAPI・実画面の確認は[Webの運用](../jobs/OPERATIONS.md#web-serving)へ。

実データ・認証を使う場合は[実行前の確認](../jobs/OPERATIONS.md#actions-write-boundary)と[設定例](../.env.sample)を使う。指標・取得方法を変えた場合の保存観測の確認は[経済原本](../jobs/OPERATIONS.md#指標観測とregistryの修正)へ。
