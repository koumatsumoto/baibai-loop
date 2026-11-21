# 開発と検証

## 手元で始める

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
| 受領の読戻しとseal | [公開前は消費せず、同じ原本と投影でsealする](../tests/jobs/publication/test_tradingview_verification.py) |
| 探索索引の問いとページ送り | [入力不足・分類・固定した個社比較を区別する](../tests/engine/investment_judgment/workflow/test_discovery_questions.py) |
| 提案の指値・価格・数量 | [観測価格と指値を分け、未確定の根拠で数量を作らない](../tests/engine/investment_judgment/trade_proposal/test_conditional_buy_terms.py) |

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
