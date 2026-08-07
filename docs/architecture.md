---
title: "Architecture"
summary: "Baibai Loop の package、store、CLI、read-only app 契約の正本。"
doc_type: architecture
status: active
---

# Architecture

Baibai Loop は単一 distribution の中で、唯一の writer である `baibai_engine` と、read-only UI を提供する `baibai_app` を分離する。application data は application DB、再生成可能な分析結果は専用 store、method / config は Git を正本とする。

```text
baibai-loop
├── baibai_engine
│   ├── foundation / market / macro / screening / research / position
│   ├── tasks / operation / proposals
│   ├── appdb / read_api
│   └── baibai-engine CLI
├── baibai_app
│   └── 127.0.0.1 固定の read-only API / UI
├── data
│   ├── app/baibai.sqlite
│   ├── screening/market.sqlite
│   ├── screening/runs.sqlite
│   └── indicators/macro.sqlite
└── method
    ├── screening-rules
    ├── macro-panel.yaml
    ├── macro-reading
    └── playbooks
```

<a id="repository-map"></a>

## Package map

| package | responsibility | public surface |
| --- | --- | --- |
| `foundation` | 共通 primitive と境界 utility | engine 内部 |
| `market` | market price / calendar の取得と L1 SQLite | engine 内部 |
| `macro` | indicator series（L1）、macro reading（L2）、published macro context（L3） | `baibai-engine macro` |
| `screening` | screening run、machine selection、shortlist、calibration | `baibai-engine screening` |
| `research` | opportunity workspace、thesis / thesis review、planning-only limit | `baibai-engine research` |
| `position` | event replay、draft / apply、holding review、outcome | `baibai-engine position` |
| `tasks` | task current state | `baibai-engine task` |
| `operation` | 1 trigger の current workspace と immutable final result | `baibai-engine operation` |
| `proposals` | trade proposal と人間の current decision | `baibai-engine proposal` |
| `appdb` | application DB path、migration、backup、writer connection | `baibai-engine db` |
| `read_api` | app が使う query-only view | engine 内部 |
| `baibai_app` | Dashboard / Macro / Stocks の read-only UI | `baibai-app` |

engine 内の domain は app に依存しない。app が DB へ触れる経路は `read_api` と query source だけで、その不変条件は[Read-only app invariants](#read-only-app-invariants)を正本とする。

`read_api` の store 欠損時の扱いは 1 つの規則で決まる: **publish 済みの内容を答える reader は空 view へ degrade し、書き込みを門番する reader は raise する**。前者は `read_rows` を通し、file 欠損と table 欠損（= writer がこの copy でまだ走っていない）を空として扱う。列名の誤り・構文エラー・store 破損は degrade せず raise するので、壊れた query が同じ沈黙に隠れない。後者は日次 batch の `market_calendar_business_day` と `previous_run_revision_id` で、休場日に見えて run を skip するのでなく故障を名指しして止まる。この規則は `tests/test_read_api_degrade.py` が全 public reader を走査して守る。

## Store contract

| store | classification | contents | write owner |
| --- | --- | --- | --- |
| `data/app/baibai.sqlite` | canonical application DB | task、macro context、shortlist、thesis revision、holding review、proposal、ledger event / price / meta、outcome、operation session | `baibai-engine` application service |
| `data/screening/market.sqlite` | rebuildable L1 | J-Quants / EDINET / JPX の price、calendar、financial input | market / screening provider |
| `data/screening/runs.sqlite` | rebuildable L2 run store | 最新数世代を保持するprunable screening run / machine selection cache | screening service |
| `data/indicators/macro.sqlite` | rebuildable L1 | provider 別 macro indicator series。manual 観測は git seed から同期 | macro indicator service |

application DB の default path は `data/app/baibai.sqlite` で、`BAIBAI_DB` または各 CLI の `--db` で差し替えられる。手動 backup は `baibai-engine db backup` を使う。自動 backup、世代管理、監査 table、transition history は持たない。

Git に残す `method/` は screening rules・Macro panel・macro reading rules の method/config、`method/playbooks/` は research checklist である。application data を GitHub Issue や YAML file に複製しない。

## Stable CLI

public entry point は次の2本だけである。

- `baibai-engine <domain> <command>`: query と application service 経由の write
- `baibai-app`: local read-only UI

主要 domain は `screening / macro / operation / position / proposal / research / task / db`。schema field、option、stdout YAML は public `--help` と engine modelを正とする。screening `run / select / ticker-profile` の YAML view は AI 向け安定契約であり、保存先が SQLite でも field の意味を変えない。

## Application data semantics

- canonical entity の作成・更新は DB transaction 内で current source と domain invariant を検証する。
- thesis / thesis review と holding review は immutable revision。source thesis revision への束縛を弱めない。
- proposal は `pending / approved / deferred / rejected` の current stateだけを持つ。broker factは人間報告後だけledger draftへ変換できる。
- ledger は append-only eventを `(occurred_at, same_instant_order)` でreplayする。既存event IDとlegacy decision referenceは保存し、新規eventを遡及挿入してcurrent snapshotを再計算できる。
- canonical ledger mutationは draft生成と、人間確認後の `position apply-draft --confirmed` を分離する。applyはexpected append head、proposal / reservation binding、置換対象rowを同一transactionで再検証する。
- operation sessionは5 kindの全体でactive最大1件。active rowのcurrent payloadを置換し、complete時に同じrowをimmutable final recordにする。checkpoint historyやtransition logは持たない。

## Read-only app invariants

`baibai-app` は `127.0.0.1` にだけbindし、write endpoint、migration、external network clientを持たない。application DB / run store / macro storeをSQLite read-only modeで開く。UIの面は8つで、3タブ（`/` Dashboard、`/macro` Macro、`/stocks` Stocks）、タブなし詳細（`/macro/reports/:contextId` Macro report、`/stocks/shortlist` Shortlist、`/stocks/assessments/:assessmentId` Bargain assessment、`/securities/:ticker` Security detail）、ヘッダーの歯車から入る運用状態画面（`/system` System）である。proposal全state、operation active/completed、portfolio outcomeをquery-only viewで表示する。Dashboardは前営業日の機械実行との差分（候補プールの出入り、機械E[r]の変化、FVに達した保有、macro readingの注記と分布の端の遷移）を観測として1区画に出す。判定・推奨は持たず、答えられなかった区分を明示して空欄と未計測を区別する。Macroは経済分析レポートと、全登録系列を`method/macro-panel.yaml`の7 groupへ配した1つのマクロ経済指標一覧（`/api/macro`のチャートと`/api/macro/reading`の記述統計を`series_id`でjoinし、取得失敗・stale・履歴不足・分布の端の件数を上部の要約カードへ畳む）、Stocksは深掘りshortlistと機械screeningのCandidatesを表示する。Shortlist は `reports/data/er-level-calibration-latest.yaml` が有効な間だけ、候補 E[r] の historical quintile と同帯の実現 total-return 中央値を文脈表示する。Candidatesはrun storeまたはクラウドの31日履歴から日付を選べる。`/api/meta`はscreening / macro / application DBのas-of鮮度と最新データ時刻をstore内timestampから返し（file mtimeに依存しない）、共通ヘッダーはUI build時刻と最新データ時刻だけを表示する。

## Cloud serving layer

クラウド閲覧と日次機械工程は、ローカルのwriter/read-only境界を変えずに次の一方向経路で構成する。

```text
local baibai.sqlite ──publish──┐
                              v
GitHub Actions compute <──> R2 baibai-stores
          │                    market/runs/macro正本 + baibai replica
          │ materialize
          v
R2 baibai-serving ──binding──> Cloudflare Worker ──> browser
views + history + system        Bearer認証 + static UI
```

- `baibai-stores` は `market.sqlite`、`runs.sqlite`、`macro.sqlite` のクラウド正本と、ローカル正本である`baibai.sqlite`のreplicaを保持する。public accessを持たない。
- `baibai-serving` は材料化済み`views/`、`history/`、`system/`の3 prefixだけを保持する。`system/latest-run.json`が`views/`の外に居るのは、`views/`が毎回のexportで作り直されるためで、exportに到達しなかった失敗runの記録はそこに置くと消える（R2 lifecycleもprefix指定で作り、bucket全体のruleを置かない）。`history/candidate-views/`には機械runをUI用の型付きread modelへ変換した履歴を置き、R2 lifecycleで31日後に削除する。`history/longlists/`には日次の明示的なlonglist membershipを置き、着手遅延計測のため400日保持するがWorker routeでは公開しない。bucket自体はpublic accessを持たず、認証済みWorkerだけがCandidatesの日付一覧と日付指定履歴をread-onlyで返す。
- WorkerのR2 bindingは`baibai-serving`だけに限定する。`/api/*`は固定Bearer passwordをSHA-256後に定数時間比較し、有限のrouteから`views/`、日付形式を検証した`history/candidate-views/`、および`system/latest-run.json`の3系統のkeyへ写像する。stores と旧形式の`history/candidates/`、`history/longlists/`には到達しない。API応答は`Cache-Control: no-store`で、CORSを有効化しない。
- Workers Assetsは`ui/dist`を無認証で配信する。bundleは業務データを含まず、実データは認証済みAPIだけから取得する。HTTP navigationはWorkerが認証処理前にHTTPSへredirectし、HTTPS応答はHSTSを持つ。
- `cloud-materialize`はapplication dataの手動publishを材料化し、`cloud-daily-batch`は平日夕方のcronで機械工程を実行し（時刻の実値と根拠は[`tools/cloud/README.md`](../tools/cloud/README.md)）、`cloud-history-backfill`は指定窓のmarket履歴を補完する。3 workflowは`cloud-publish`の`queue: max`を共有し、pending writerをFIFOで保持しながらrunning/uploadを1件に限定する。
- ローカル`pull`はmachine storeだけを置換し、canonical application DBを上書きしない。ローカル`publish`はSQLite snapshotをstoresへ置き、materializeをdispatchする。

具体的な初期構築、publish/pull、手動再実行、password rotationは[`tools/cloud/README.md`](../tools/cloud/README.md)を正本とする。

## Data layers

| layer | examples | rule |
| --- | --- | --- |
| L1 fact | market price、calendar、macro series | provider由来を保持し、再取得可能なstoreへ置く |
| L2 machine analysis | screening run、E[r]、FV anchor、machine selection、macro reading | observed / derived / estimateを区別し、judgmentと呼ばない |
| L3 judgment / operation | macro context、shortlist、research、proposal、ledger、task、operation | application DBを正本にし、人間境界をwrite-timeに検証する |

fact / estimate / judgment の語彙と禁止事項は [`doctrine.md#fact-analysis-separation`](./doctrine.md#fact-analysis-separation) を正本とする。

## Development gates

機械契約はDB constraint、pydantic model、application service validationとnegative testで守る。write 時の検証層は次の4つ:

| layer | responsibility |
| --- | --- |
| SQLite constraint / trigger | required identity、enum、foreign key、immutable row、active最大1件 |
| pydantic / domain model | field type、shape、cross-field invariant |
| application service | current source、revision、proposal / reservation、人間確認、stale no-write |
| config loader | Git管理のscreening rules、Macro panel config、playbookの構造 |

高影響のDB変更では正常系だけでなく、conflicting ID、invalid enum、missing reference、revision drift、stale draft、人間確認なし、read-only appからのwrite不能をtestする。ledgerはevent順序、snapshot全field、market price / override / metaをfixtureと比較する。schema fileやlive YAML treeを横断するvalidator CLIは置かず、保持すべきruleは各write pathのnegative testで反証する。

通常のgateは `ruff format --check`、`ruff check`、`mypy`、`pytest`、import-linter、frontend build。Python gate の完全形と再現手順は [`reference/python-foundation.md`](./reference/python-foundation.md) §9、UI・Cloudflare Worker・security（Bandit / pip-audit / npm audit）を含む全 CI job は `.github/workflows/`（`ci.yml` / `web.yml` / `security.yml`）を正本とする。設定fileはloader testで検証する。
