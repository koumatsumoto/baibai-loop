# 開発と検証

## 手元で始める

初めて作業コピーを作るときは、`main` の先端だけを取得する。

```bash
git clone --depth=1 --single-branch --branch main https://github.com/koumatsumoto/baibai-loop.git
cd baibai-loop
```

過去の差分を調べるときだけ、履歴の浅い作業コピーで `git fetch --deepen=50 origin main` を実行する。`main` の全履歴が必要なら `git fetch --unshallow origin` を使う。既存の作業コピーを作り直す必要はない。

Linux / Bash、bootstrap用Python 3.11以上、uvを用意し、リポジトリのルートで実行する。利用版は[.python-version](../.python-version)・[pyproject.toml](../pyproject.toml)、依存は[uv.lock](../uv.lock)で固定する。

```bash
bash tools/setup-dev.sh
source .cache/dev/env.sh
uv run baibai-engine --help
uv run baibai-jobs --help
```

shellを開き直したらenv.shをsourceする。checkoutを移動したらsetupをやり直す。通常のfixture・CLI helpに.envや本番storeは不要。ブラウザの検証が必要なときだけsetupに `--with-browser` を付ける。

## 変更を検証する

[責務と変更先](architecture.md#変更する場所)を確認し、変更したdomainと、そのdomainを呼ぶCLI/Jobsのテストを選ぶ。例は一時DBを使うローカル検証。

```bash
uv run pytest -n 0 tests/engine/primitives
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
```

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

[.node-version](../.node-version)のNodeと対応するnpmで検証する。

```bash
npm --prefix web/frontend ci
npm --prefix web/edge ci
npm --prefix web/frontend run build
```

`npm --prefix web/frontend run dev` は画面資産のみを配信し、ローカルAPIやSQLiteには接続しない。表示データの生成・認証付きAPI・実画面の確認は[Webの運用](../jobs/OPERATIONS.md#web-serving)へ。

実データ・認証を使う場合は[実行前の確認](../jobs/OPERATIONS.md#actions-write-boundary)と[設定例](../.env.sample)を使う。指標・取得方法を変えた場合の保存観測の確認は[経済原本](../jobs/OPERATIONS.md#指標観測とregistryの修正)へ。
