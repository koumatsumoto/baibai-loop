---
title: "Architecture"
summary: "Baibai Loop の package、store、CLI、read-only app 契約の正本。"
doc_type: architecture
status: active
---

# Architecture

Baibai Loop は単一 distribution の中で、唯一の writer である `baibai_engine`、read-only presentation の `baibai_web`、non-request-driven orchestration の `baibai_batch` を分離する。application data は application DB、再生成可能な分析結果は専用 store、production methodology と presentation config は Git を正本とする。

実行・開発・運用環境は Ubuntu Linux のみをサポートする。CI と運用scriptも Ubuntu、POSIX path、GNU/Linux filesystem primitive を前提とし、Windows / macOS 向けの互換層は持たない。

```text
baibai-loop/
├── engine/src/baibai_engine/
├── web/
│   ├── backend/src/baibai_web/
│   ├── frontend/
│   ├── edge/
│   ├── contracts/
│   └── config/
├── batch/src/baibai_batch/
├── tools/
├── method/{macro,screening,research}/
├── stores/{application,market,macro,screening}/
├── reports/{studies,published,operations}/
├── docs/
├── .agents/
└── .github/
```

<a id="repository-map"></a>

## Package map

| package | responsibility | public surface |
| --- | --- | --- |
| `foundation` | 共通 primitive と境界 utility | engine 内部 |
| `market` | market fact の取得、L1 SQLite、immutable lake contract、固定 release からの store hydration | `baibai-engine lake` |
| `macro` | indicator series（L1）、macro reading（L2）、published macro context（L3） | `baibai-engine macro` |
| `screening` | screening run、Opportunity Lane selection、Attention Policy、Review Set、Research Gate、shortlist、calibration | `baibai-engine screening` |
| `research` | opportunity workspace、thesis / thesis review、planning-only limit | `baibai-engine research` |
| `position` | event replay、draft / apply、holding review、outcome | `baibai-engine position` |
| `tasks` | task current state | `baibai-engine task` |
| `operation` | 1 trigger の current workspace と immutable final result | `baibai-engine operation` |
| `proposals` | trade proposal と人間の current decision | `baibai-engine proposal` |
| `appdb` | application DB path、migration、backup、writer connection | `baibai-engine db` |
| `read_api` | app が使う query-only view | engine 内部 |
| `baibai_web` | Dashboard / Macro / Stocks の read-only UI | `baibai-web` |
| `baibai_batch` | scheduled/offline job、store transfer、validation、observability | repository-internal `baibai-batch` |

engine は web / batch / tools に依存しない。Web が engine へ触れる経路は `read_api`、batch は `batch_api` と `read_api` に限定し、その不変条件は import-linter で検査する。read-only Web の実行時契約は[Read-only app invariants](#read-only-app-invariants)を正本とする。

`read_api` の store 欠損時の扱いは 1 つの規則で決まる: **publish 済みの内容を答える reader は空 view へ degrade し、書き込みを門番する reader は raise する**。前者は `read_rows` を通し、file 欠損と table 欠損（= writer がこの copy でまだ走っていない）を空として扱う。列名の誤り・構文エラー・store 破損は degrade せず raise するので、壊れた query が同じ沈黙に隠れない。後者は日次 batch の `market_calendar_business_day` と `previous_run_revision_id` で、休場日に見えて run を skip するのでなく故障を名指しして止まる。この規則は `tests/engine/test_read_api_degrade.py` が全 public reader を走査して守る。

## Store contract

| store | classification | contents | write owner |
| --- | --- | --- | --- |
| `stores/application/baibai.sqlite` | canonical application DB | task、macro context、shortlist、thesis revision、holding review、proposal、ledger event / price / meta、outcome、operation session | `baibai-engine` application service |
| `stores/market/market.sqlite` | mixed authority（lake所有17 data tableはL1 releaseからのruntime copy、残る2 data tableはここがcanonical、`lake_store_origin`はstore-local metadata） | J-Quants / EDINET / JPX の price、calendar、financial input と、取得範囲の帳簿・資本配分・支配権イベントの typed fact | market / screening provider |
| `stores/screening/runs.sqlite` | rebuildable L2 run store | 最新数世代を保持するprunable screening run / machine selection cache | screening service |
| `stores/screening/calibration/` | rebuildable L2 analytical bundle | typed Parquet の calibration panel / diagnostics / forward outcome と、3 datasetを原子的に束ねるbundle manifest・pointer | screening calibration service |
| `stores/macro/macro.sqlite` | rebuildable L1 | provider 別 macro indicator series。manual 観測は git seed から同期 | macro indicator service |

application DB の default path は `stores/application/baibai.sqlite` で、`BAIBAI_DB` または各 CLI の `--db` で差し替えられる。未適用 migration があるときだけ、最初の文の前に checkpoint を自動で取り、直近 10 世代を残す（手動で取るときは `baibai-engine db backup`）。R2 側は `baibai.sqlite.bak-YYYYMMDD` で 1 日 1 世代を直近 14 世代まで残す。監査 table と transition history は持たない — 復元点は store の copy であって、行ごとの履歴ではない。

Git に残す `method/` は `screening/rules`、`macro/reading`、`research/playbooks` の production methodology である。Macro panel の表示 group は `web/config/macro-panel.yaml` が所有する。application data を GitHub Issue や YAML file に複製しない。

### Market lake publication contract

大規模な market fact は、R2 の不変 object を Parquet で保持し、dataset manifest と L1 release
manifest で exact input generation を固定する。DuckDB は Parquet の build・validation・analysis
だけを担い、常駐 server や唯一の永続 DB にしない。SQLite は application state、小規模な関係
data、固定 release から再構築できる runtime copy に限定する。Web は L1 を直接読まず、
materialized read model だけを読む。

| layer | canonical form | allowed contents |
| --- | --- | --- |
| L1 Canonical | Parquet object + dataset / release manifest | typed source fact、source identity、publication / effective / retrieved time、revision semantics |
| L2 Analytical | Parquet object + dataset manifest + atomic bundle pointer | 再生成可能な panel、feature、forward outcome |
| L2 Operational / L3 | SQLite | run metadata、selection、thesis、proposal、ledger、operation 等の transaction / point lookup state |

R2 key は `lake/` 以下だけを使い、segment allowlist で path traversal を拒否する。time-series
partition は `year/month`、file は ZSTD Parquet、object name は content SHA-256 とする。dataset
manifest は全 partition object と totals を列挙し、L1 release manifest は互換な dataset build の
組を一つの `release_id` へ固定する。logical object identity は key・SHA-256・bytes・rows・schema
で決まり、object-store固有のETagはpublish/CASのtransport stateにだけ置く。lineageはtyped `SourceRef`で表す。kindは**bytesを保持するかどうか**の2族に分かれ、
それが型の違いになる。

- **retained**（`l1_release`）はlake内のkeyを名乗る。resolverはkey・SHA-256・source側versionと
  release closureを検証する。ただし到達可能性はcurrent releaseのretention policyに従い、分析成果物が
  過去releaseを名乗っただけで恒久保持されるわけではない。
- **identity only**（`sqlite_snapshot`）はkeyを持たない。sealed snapshotはbuild中にstoreが動かない
  ようにするためのもので、その役目はbuildの終わりで終わる。bytesはlegacy store全体（約2GB）なので、
  buildごとに1つ保持すればlakeはpublishした量ではなくrun回数に比例して育つ。よってschema version・
  content digest・capture時刻だけを残し、bytesはoperationの終わりで回収する。
  同じ`source_id`を名乗る2つのbuildは同一入力を読んでおり、rebuildへ差し出されたstore世代はこの
  digestで照合できる。**保証しないのは、その世代がまだ入手できること**である。

`SourceRef`（buildが自分の入力について述べるunion）に入るのは`sqlite_snapshot`だけである。buildが
読むのはsealed storeであってreleaseではないからで、closure resolverの有無ではなく何を読んだかが
決めている。`CohortSourceRef`は既存のimmutable v1 manifestを読むため`l1_release`も受け入れるが、
calibrationの現行writerはsnapshotだけを記録する。L1は`source_coverage`などの非lake入力を保持しない
ため、release refをcalibration inputの完全再構築保証には使わない。release manifestはobject graphの
rootにすぎないので、resolverはdataset manifestとParquet objectまで歩いて全部digestで検証し、
歩き切れないrefは解決しない。

manifestとpointerを含むlake JSONは、duplicate key拒否とredacted validation errorを持つ
共通parserだけを通し、wire size上限をparse前に検査する。partition valuesとrelease dataset
inventoryはparse後に変更できない。
releaseはprofileを宣言し、そのprofileのmanifest size/object budgetと、dataset ごとのrequired・
accepted contract・coverage要求・rows / population floor・検証時刻基準のfreshness窓を満たす場合だけ
current候補になる。cadenceも完全性もdatasetの性質なので、profile単位の単一閾値は持たない。
profileは`production`ひとつで、要求の集合がひとつだからである。登録の無いprofileはfail-closeする。

version 語彙は `contract_version`（schema・PK・型・partition・意味の互換境界）、`build_id`
（immutable build）、typed `SourceRef`内のsource側version、`producer_git_commit`（code identity）
に限定する。同じ contract 内の logic / config / 明示したtransform source codeは
`transform_fingerprint`で識別する。
L2 calibrationのlineageはdataset全体のsource集合ではなくcohort inventoryの各roleへ置き、panel /
diagnosticsのcohort cutoffとforwardのobservation cutoffをsource digestと一緒に固定する。
fingerprintはschema/configだけでなく、そのdatasetの値を決めるsemantic implementation fileのdigestを含む。
production reader は期待する contract 一つだけを受け入れ、schema change は in-place migration
や `union_by_name` fallback ではなく、新しい contract の immutable rebuild と pointer switch で
扱う。

一つの dataset が同時に二つの canonical writer を持たない。市場 fact の canonical authority は
R2 の L1 release にあり、`market.sqlite` はその fixed release から削除・再構築できる runtime copy
である。lakeが持たない2 data table — 取得範囲の帳簿と、月次snapshotのoperator導出fact — だけが
SQLiteをcanonicalとする。R2が持つstoreのcopyはその2 data tableと、store-local publication metadata
`lake_store_origin`を運ぶ。full-file publish は行わない。

読み取り側は実行開始時に current pointer を 1 度だけ解決し、以後は固定した `release_id` と
immutable object key だけを読む。manifest digest、object digest、dataset contract の不一致は
fail-close で、prefix listing・glob・`union_by_name` による吸収・provider fallback はいずれも
持たない。固定 release を SQLite へ実体化するのは `lake hydrate` で、store の sealed copy へ
lake 所有 table だけを積み直し、single rename で publish する。読み込んだ行数が release manifest の
publish 行数と一致しなければ fail-close する — 静かに空のまま進んだ store は、screening に空の
universe を健全な結果として publish させるためである。手順は
[`reference/market-lake.md`](./reference/market-lake.md#fixed-release-read) を正本とする。

このcustom manifest protocolは、単一writer・小規模catalog・Python中心という現在の制約に対して
table formatより小さい。次のいずれかが現れた時点で、Apache Iceberg / R2 Data Catalog等への
置換を再評価する: 同時writerが2以上になる、object数が10万を超える、schema branchを複数同時に
維持する、dataset横断のsnapshot transactionが要る、remote GCを自前で持つ、row-level mutationが要る。
どれも現状は無く、無い間は自前protocolの方が状態空間が小さい。

## Stable CLI

安定した利用者向け entry point は次の2本である。

- `baibai-engine <domain> <command>`: query と application service 経由の write
- `baibai-web`: local read-only UI

`baibai-batch` は GitHub Actions と運用 script が production job を呼ぶための
repository-internal entry point で、domain の利用者向け surface ではない。

主要 domain は `lake / screening / macro / operation / position / proposal / research / task / db`。
`lake inventory` は local R2 mirror の metadata だけを読み、`lake validate` は JSON manifest
contract だけを検査して object の dereference・publish・rewrite をしない。`lake resolve` は
current pointer を 1 度だけ解決して固定 release の identity を出し、`lake hydrate` はその release
から market store の lake 所有 table を満たす。どちらも immutable object を書き換えない。
`lake gc` は root closure から削除候補と plan hash を出す
（既定は dry-run で、`--apply` は同じ plan hash を要求する）。
schema field、option、stdout YAML は public `--help` と engine modelを正とする。screening `run /
select / ticker-profile` の YAML view は AI 向け安定契約であり、保存先が SQLite でも field の
意味を変えない。

## Application data semantics

- canonical entity の作成・更新は DB transaction 内で current source と domain invariant を検証する。
- thesis / thesis review と holding review は immutable revision。source thesis revision への束縛を弱めない。
- proposal は `pending / approved / deferred / rejected` の current stateだけを持つ。broker factは人間報告後だけledger draftへ変換できる。
- ledger は append-only eventを `(occurred_at, same_instant_order)` でreplayする。既存event IDとlegacy decision referenceは保存し、新規eventを遡及挿入してcurrent snapshotを再計算できる。
- canonical ledger mutationは draft生成と、人間確認後の `position apply-draft --confirmed` を分離する。applyはexpected append head、proposal / reservation binding、置換対象rowを同一transactionで再検証する。
- operation sessionは5 kindの全体でactive最大1件。active rowのcurrent payloadを置換し、complete時に同じrowをimmutable final recordにする。checkpoint historyやtransition logは持たない。

Opportunity Discoveryからproposalまでの状態遷移は次の責務境界を持つ。

```text
universe → candidates                 screening
candidates → Lane Longlists           Selection Policies
Lane Longlists → Review Set           Attention Policy
Review Set → Shortlist                Research Gate
Shortlist → Primary Research Set      human admission
Primary Research Set → Thesis         research + review
Theses → Bargain Assessment           Assessment Casesの統合判断
Bargain Assessment → Trade Proposal   human decision input
```

異なるEconomic Hypothesisを採用するときは別Opportunity Laneとし、同一scoreへ畳まない。Selection PolicyがLane内のnomination / ordering、Attention PolicyがLane間のallocation、Research Gateがresearch-worthiness judgment、人間がPrimary Research Setへのadmissionを所有する。この admission 境界は記述だけでなく機械的に強制する — `research prepare --shortlist-id` がcanonical Shortlistへ束縛し、researchできるのはいずれかのpublished Research Gateが`selected`としたtickerに限られる（[`screening-runtime.md`](./reference/screening-runtime.md)）。現行wireは、採用済みValue / Carry Laneの`longlist`を`value-carry-only-v1`が`review_tickers`へ写す最小構成であり、generic registry・executor・Dynamic Attention Composerは持たない。Earnings Power Laneは固定replayが`inconclusive`だったためproduction wireへ採用していない（[`historical-replay.yaml`](../reports/studies/2026-08-24-earnings-power-v1/historical-replay.yaml)）。

## Read-only app invariants

`baibai-web` は `127.0.0.1` にだけbindし、write endpoint、migration、external network clientを持たない。application DB / run store / macro storeをSQLite read-only modeで開く。UIの面は8つで、3タブ（`/` Dashboard、`/macro` Macro、`/stocks` Stocks）、タブなし詳細（`/macro/reports/:contextId` Macro report、`/stocks/shortlist` Shortlist、`/stocks/assessments/:assessmentId` Bargain assessment、`/securities/:ticker` Security detail）、ヘッダーの歯車から入る運用状態画面（`/system` System）である。proposal全state、operation active/completed、portfolio outcomeをquery-only viewで表示する。Dashboardは前営業日の機械実行との差分（候補プールの出入り、機械E[r]の変化、FVに達した保有、macro readingの注記と分布の端の遷移）を観測として1区画に出す。判定・推奨は持たず、答えられなかった区分を明示して空欄と未計測を区別する。Macroは経済分析レポートと、全登録系列を`web/config/macro-panel.yaml`の7 groupへ配した1つのマクロ経済指標一覧（`/api/macro`のチャートと`/api/macro/reading`の記述統計を`series_id`でjoinし、取得失敗・stale・履歴不足・分布の端の件数を上部の要約カードへ畳む）、Stocksは深掘りshortlistと機械screeningのCandidatesを表示する。Shortlist は `reports/published/er-level-calibration-latest.yaml` が有効な間だけ、候補 E[r] の historical quintile と独立した要求利回りhurdle以上帯について、実現 total-return の中央値・下方分位・trap率を文脈表示する。Candidatesはrun storeまたはクラウドの31日履歴から日付を選べる。`/api/meta`はscreening / macro / application DBのas-of鮮度と最新データ時刻をstore内timestampから返し（file mtimeに依存しない）、共通ヘッダーはUI build時刻と最新データ時刻だけを表示する。

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
- Workers Assetsは`web/frontend/dist`を無認証で配信する。bundleは業務データを含まず、実データは認証済みAPIだけから取得する。HTTP navigationはWorkerが認証処理前にHTTPSへredirectし、HTTPS応答はHSTSを持つ。
- `cloud-materialize`はapplication dataの手動publishを材料化し、`cloud-daily-batch`は平日夕方のcronで機械工程を実行し（時刻の実値と根拠は[`batch/OPERATIONS.md`](../batch/OPERATIONS.md)）、`cloud-history-backfill`は指定窓のmarket履歴を補完する。3 workflowは`cloud-publish`の`queue: max`を共有し、pending writerをFIFOで保持しながらrunning/uploadを1件に限定する。
- ローカル`pull`はmachine storeだけを置換し、canonical application DBを上書きしない。ローカル`publish`はSQLite snapshotをstoresへ置き、materializeをdispatchする。

具体的な初期構築、publish/pull、手動再実行、password rotationは[`batch/OPERATIONS.md`](../batch/OPERATIONS.md)を正本とする。

## Data layers

| layer | examples | rule |
| --- | --- | --- |
| L1 fact | market price、calendar、macro series | provider由来とsource identityを保持し、SQLiteまたはimmutable Parquetの一意なauthorityへ置く |
| L2 machine analysis | screening run、E[r]、FV anchor、machine selection、macro reading、analytical Parquet | observed / derived / estimateを区別し、judgmentと呼ばない |
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
