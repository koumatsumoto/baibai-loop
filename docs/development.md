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

PR全体の検査は[Python CI](../.github/workflows/ci.yml)と[Web CI](../.github/workflows/web.yml)が正本。通常testは実networkを禁止し、CIの緑やfixtureの成功を本番受入には数えない。R2 integration testだけは明示有効化時に書込・削除するため、依頼された専用の空bucketでのみ `-n 0` で行う。

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
