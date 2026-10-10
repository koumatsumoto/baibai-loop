---
title: "Python foundation"
summary: "repository固有の開発環境、クラウドとActionsの分担、validationとtest、完全gate。"
doc_type: reference
status: active
source_paths:
  - "../../engine/src/baibai_engine/"
  - "../../web/backend/src/baibai_web/"
  - "../../batch/src/baibai_batch/"
  - "../../tests/"
  - "../../pyproject.toml"
  - "../../uv.lock"
  - "../../tools/setup-dev.sh"
---

# Python foundation

## 1. Runtime policy

対応環境は[architecture](../architecture.md)に従う。Pythonの許容範囲は`pyproject.toml`、実行patchは`.python-version`、Node.jsはWeb workflowを正本とする。

## 2. Dependency policy

依存は`pyproject.toml`と`uv.lock`で管理する。開発環境は`uv sync --frozen --all-groups`で揃える。MCP SDKはOwner MCPとTradingView collectorで共用するruntime依存とする。

<a id="cloud-development-boundary"></a>

### クラウド開発とGitHub Actionsの分担

ChatGPTクラウド環境は、外部service・本番storeに対して基本read-onlyで使う。
コード・文書の編集、依存導入、build、fixture test、使い捨ての検証DBへの書込、commit・push・PR作成は開発作業として行う。
外部への書込、production dataの更新・公開、provider取得とOAuth state更新、deployはGitHub Actionsへ集約する方針とし、
移行済みの入口と残るlocal経路は[batch運用の実行境界](../../batch/OPERATIONS.md#actions-write-boundary)で確認する。
read-onlyはGit作業や検証用filesystemを禁止する意味ではなく、本番資格情報の権限境界である。

通常の流れは、保存済み環境でcheckoutと依存を準備 → branchで修正 →
[完全gate](#9-ci-and-local-parity)と必要なread-only実データ確認 → draft PR → 対象commitのCI確認とする。
実行済み・失敗・未実行をPRへ記し、fixture成功を実データや本番反映の成功に読み替えない。
main反映後の本番操作が依頼範囲に含まれる場合だけ、既存workflowのref・入力・対象を確認して起動し、
run IDを固定して結果と公開物を検証する。対応workflowがない操作はクラウドから直接書かず、移行が必要と報告する。

公開repositoryのstandard GitHub-hosted runnerの実行時間は無料なので、CIと承認された運用に積極的に使う。
larger runner、artifact/cache等のstorage、外部API、R2の利用まで一律無料とは扱わない。
料金の最新条件は[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)を確認する。

<a id="development-bootstrap"></a>

### 繰り返し使う開発環境の準備

Linux / Bash、Python 3.11以上（bootstrap用）、uv、Web workflow指定majorのNode.jsとnpmを用意し、
対象revisionをcheckoutしてから実行する。scriptは配置場所からrepository rootを解決する。

```bash
bash tools/setup-dev.sh
source .cache/dev/env.sh
uv run baibai-web serve
```

scriptはcheckout指定のuvとPythonを`.cache/dev/`へ用意し、rootの全dependency group、
frontendとedgeのlockfile依存、frontend buildを準備する。再実行時は生成物を更新する。
checkout、lockfile、`.env`、storeは変更しない。生成したenv.shには非secretのpath設定だけを置き、
shellごとにsourceする。checkoutを移動した場合はsetupを再実行する。
基本setupには秘密情報も実データも不要で、lint・型検査・fixture testと空データのUIが使える。
品質検証は[完全gate](#9-ci-and-local-parity)、UIの操作は[Web](../../web/README.md)を参照する。

追加downloadは必要な場合だけ明示する。失敗時は非zeroで終了し、基本setup成功と追加機能の導入成功を区別する。

```bash
bash tools/setup-dev.sh --with-browser --with-httpfs
```

ChromiumはPlaywright指定版、httpfsは既存provision toolを使う。OSのbrowser用libraryと
DuckDB既定extension cacheの書込権限は環境側で用意する。403なら実際の配布hostを環境管理側で確認し、
proxy解除・TLS検証無効化で回避しない。書込可能な別directoryへのDuckDB接続設定は新規connectionへ継承されず、
現在のlake readerにはそのpathを渡す環境変数が無い。任意pathへの配置だけで導入完了にしない。
既存ChromiumをUI smokeだけに使う場合は実行pathとversionを確認し、Playwrightの
`chromium.launch(executable_path=..., headless=True)`で明示する。指定版の導入・互換性検証とは区別する。

### 保存済みクラウド環境への登録

Install scriptは、対象checkoutへ移動して `bash tools/setup-dev.sh` を実行する。
scriptを含むrevisionがcheckoutされることが前提で、一時タスクの`/workspace`ファイルは参照しない。
未反映のworking treeだけにscriptがある間は、新規タスクで利用可能とは扱わない。

Start skillには次を登録する。

> 実際のcheckout rootとAGENTS.mdを確認する。各shellでrepository rootへ移動し、
> `.cache/dev/env.sh` がなければ `bash tools/setup-dev.sh` を実行し、失敗した場合は停止する。
> ファイルが既存の場合もsetupで生成した場合も、続けて `source .cache/dev/env.sh` を実行する。
> 検証はPython foundationの完全gateに従い、実行済み・失敗・未実行を区別する。
> UIが必要なら `uv run baibai-web serve` を起動し、127.0.0.1:8712のhealthと画面を確認する。
> データ不在を実データ検証成功と扱わない。外部接続はread-onlyとし、本番書込はbatch運用の実行境界に従う。

環境を保存・再公開した後は、新規タスクでversionとfixture gateを確認する。
利用者の端末からUIを見るには認証付きport preview / forwardingを別途用意する。
Vaultの値はユーザーがruntime用secret設定へ入力し、Install script、Start skill、image snapshot、
Git、ログへ埋め込まない。`.env`やOAuth stateを含むworking directoryをsnapshotへ取り込まない。

### Personal Vaultとread-only接続

基本開発にはsecretを登録しない。実データが必要なときだけ、下表から必要な接続を選ぶ。
Personal Vaultへ保存するだけではtaskに渡らないため、環境側で同名keyをrequestし、
VaultのKeyとTypeを揃え、Applies toに対象環境を含める。保存・再公開後の新規taskで、
値を表示せず設定の有無と必要な読取の成功を確認する。
[Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)の現行設定に従う。

| 機能 | キー名・前提 |
| --- | --- |
| 基本開発・fixture・空データUI | secret不要。任意の検証コピーpathは`BAIBAI_DB` / `BAIBAI_RUNS_DB`（非secret） |
| 実データの読取 | 許可されたSQLite snapshotと固定lake releaseが必要。データがなければ未検証 |
| lakeの直接R2読取 | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`だけをEnvironment variableとしてrequestする。専用の対象bucketのObject Read only credentialとhttpfsが必要 |
| Owner MCPのL1 gateway読取 | 必要な場合だけ`READ_ACCESS_TOKEN`。[Owner MCP](../../tools/owner_mcp/README.md)参照 |
| Workerの開発用認証・L1接続検証 | 必要な場合だけ`VIEW_PASSWORD`, `READ_ACCESS_TOKEN`, `L1_R2_BASE_URL`, `L1_R2_ACCESS_KEY_ID`, `L1_R2_SECRET_ACCESS_KEY`。開発用に分離し、L1はObject Read only。[sample](../../web/edge/.dev.vars.example)参照 |

R2のS3署名にはprocessが読むraw credentialが必要なので、placeholderをHTTPS proxyが置換するNetwork secretでは代用しない。
直接のEnvironment variableはtaskから値が読めるため、writer credentialを流用せず最小権限にする。
Personal Vaultの値は接続先をnetwork allowlistへ自動追加しない。必要なhostだけ環境設定で許可し、
403等はscope・endpoint・network設定を確認する。秘密値の表示やproxy・TLS検証の解除で診断しない。

writer用R2 credential、provider API key、TradingView OAuth state・secret writer token、
Cloudflare deploy tokenはクラウド開発用Vaultへ入れない。Actions側のキー名と設定先は
[batch運用](../../batch/OPERATIONS.md#actions-write-boundary)を正本とする。
R2 acceptanceも書込・削除する外部試験なので、通常のread-onlyクラウドgateでは実行しない。
provider取得、実store validation、OAuth smoke、R2 acceptanceをfixture gate成功で代用しない。

## 3. Formatting and linting

Ruffを使う。runtimeはPython 3.14だが、formatter targetは`py313`である。複数例外捕捉を`except (T1, T2):`に揃え、PEP 758の括弧なし構文へ自動変換させないための選択である。

Markdownのhard breakを壊さないよう、trailing whitespace hookは`.md`を対象外とする。その他の設定値は`pyproject.toml`とpre-commit設定を参照する。

## 4. Type checking

CIの型検査はmypy strictに統一し、runtime packageとtoolsを対象にする。IDEの型検査は開発者の選択とし、別のCI type gateを追加しない。

## 5. Validation boundary

外部入力と保存境界をdomain model・DB constraint・serviceで検証する。providerの正規化、文書内部の整合、現在のsourceとの整合はそれぞれのownerが担う。内部計算に同じvalidationを重ねない。過去の保存物の読取と、新しい判断の適格性を区別する。

## 6. I/O and adapters

外部I/Oはadapter、domain処理はengineへ置く。保存・原子的な書込・YAML読込は既存の共通処理を使う。CLI・batch・Webがdomainの算術や保存規則を再実装しない。

## 7. Testing and coverage

domainの契約はownerのtestで確認し、CLI・batch・Webは自分の入出力と接続を検証する。同じ条件を全層で再列挙しない一方、assert本数やcase数に固定の上限は置かない。

fixtureの共通化は意味が同じ場合に行う。builderでproductionにない制約を作らない。testの削除・統合は、重複している契約と残る検出力から判断する。

coverageはpytest-covで計測する。worker数と閾値は設定・workflowを正本とし、性能変更は同じ入力・環境で比較する。過去の所要時間を現在の性能保証として常設しない。

## 8. Security checks

sourceの静的検査はBandit、依存監査はruntime/devについてlock由来のpip-audit、build-systemについて`[build-system].requires`由来のpip-audit、およびnpm auditを使う。実行環境にたまたま入ったpackageをproject依存として監査しない。advisoryはcodeが変わらなくても更新されるため、依存監査には週次実行も持つ。

CodeQLは現行構成では採用しない。scanner追加は既存検査との差分と運用価値で判断する。workflowの権限・Action固定・dispatch入力は既存trust gateが検証する。

## 9. CI and local parity

各blockはrepository rootから独立して実行し、非zeroなら原因を直してから次へ進む。実際のtrigger/stepは`.github/workflows/`を参照する。

```bash
(
  set -e
  uv sync --frozen --all-groups
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy
  uv run lint-imports
  for gate in tools/quality/drift/check_*.py; do
    uv run python -m "tools.quality.drift.$(basename "$gate" .py)"
  done
  TMPDIR=/dev/shm uv run pytest -n 4 --cov --cov-report=term-missing
  uv run bandit -c pyproject.toml -q -r engine/src/baibai_engine web/backend/src/baibai_web batch/src/baibai_batch tools
  uv export --format requirements.txt --locked --all-groups --no-emit-project --no-hashes --output-file /tmp/baibai-loop-requirements.txt
  uv run pip-audit -r /tmp/baibai-loop-requirements.txt
  uv run python - <<'PY' > /tmp/baibai-loop-build-requirements.txt
import tomllib
from pathlib import Path

for requirement in tomllib.loads(Path("pyproject.toml").read_text())["build-system"]["requires"]:
    print(requirement)
PY
  uv run pip-audit -r /tmp/baibai-loop-build-requirements.txt
)
```

Node依存監査:

```bash
(
  set -e
  (cd web/frontend && npm audit --package-lock-only --audit-level=high)
  (cd web/edge && npm audit --package-lock-only --audit-level=high)
)
```

Web UI と Worker:

```bash
(
  set -e
  (cd web/frontend && npm ci && npm run lint && npm run build && npm test)
  (cd web/edge && npm ci && npm run types:check && npm run typecheck && npm test && npx wrangler deploy --dry-run --outdir /tmp/baibai-worker-bundle)
)
```

macroの計算・取得・registry・reading methodを変更した場合は、localの対象storeも検証する。

```bash
uv run baibai-batch validate-macro-stores
```

storeがなければ未実行と報告する。CIにはapplication storeがなく、通常gateは代用にならない。文書だけの変更でlive取得・本番再発行を行わない。

documented-command gateはskillsと指定したactive runbookの単純なcommandをparserへ照合する。Lakeのread/write入口とWeb CLIも対象とする。shell置換・pipe・redirect・command連結は対象外で、実際のcommandを実行しない。対象文書の一覧は[gate](../../tools/quality/drift/check_documented_commands.py)を正本とし、gate成功を全文書の意味や複合shell例の正しさの証明としない。

## 10. Review rule

設定・workflow・本節のcommandを整合させる。通常の開発変更に別の承認手順、検査結果台帳、全domainの再検証を追加しない。
