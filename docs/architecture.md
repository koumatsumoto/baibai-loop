---
title: "Architecture"
summary: "Baibai-Loop の package、store、CLI、read-only app 契約の正本。"
doc_type: architecture
status: active
last_reviewed: 2026-07-22
---

# Architecture

Baibai-Loop は単一 distribution の中で、唯一の writer である `baibai_engine` と read-only UI「Baibai App」の `baibai_app` を分離する。application data は application DB、再生成可能な分析結果は専用 store、method / config は Git を正本とする。

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
    └── playbooks
```

<a id="repository-map"></a>

## Package map

| package | responsibility | public surface |
| --- | --- | --- |
| `foundation` | 共通 primitive と境界 utility | engine 内部 |
| `market` | market price / calendar の取得と L1 SQLite | engine 内部 |
| `macro` | indicator series と published macro context | `baibai-engine macro` |
| `screening` | screening run、machine selection、shortlist、calibration | `baibai-engine screening` |
| `research` | opportunity workspace、thesis / thesis review、planning-only limit | `baibai-engine research` |
| `position` | event replay、draft / apply、holding review、outcome | `baibai-engine position` |
| `tasks` | task current state | `baibai-engine task` |
| `operation` | 1 trigger の current workspace と immutable final result | `baibai-engine operation` |
| `proposals` | trade proposal と人間の current decision | `baibai-engine proposal` |
| `appdb` | application DB path、migration、backup、writer connection | `baibai-engine db` |
| `read_api` | app が使う query-only view | engine 内部 |
| `baibai_app` | Dashboard / Screening / Security / Macro の read-only UI（Baibai App） | `baibai-app` |

engine 内の domain は app に依存しない。app は `read_api` と query source を通じて DB を read-only mode で開き、migration、write service、外部 networkへ到達しない。

## Store contract

| store | classification | contents | write owner |
| --- | --- | --- | --- |
| `data/app/baibai.sqlite` | canonical application DB | task、macro context、shortlist、thesis revision、holding review、proposal、ledger event / price / meta、outcome、operation session | `baibai-engine` application service |
| `data/screening/market.sqlite` | rebuildable L1 | J-Quants / EDINET / JPX の price、calendar、financial input | market / screening provider |
| `data/screening/runs.sqlite` | rebuildable L2 run store | 最新数世代を保持するprunable screening run / machine selection cache | screening service |
| `data/indicators/macro.sqlite` | rebuildable L1 | provider 別 macro indicator series。manual 観測は git seed から同期 | macro indicator service |

application DB の default path は `data/app/baibai.sqlite` で、`BAIBAI_DB` または各 CLI の `--db` で差し替えられる。手動 backup は `baibai-engine db backup` を使う。自動 backup、世代管理、監査 table、transition history は持たない。

Git に残す `method/` は screening rules と Macro panel の method/config、`method/playbooks/` は research checklist である。application data を GitHub Issue や YAML file に複製しない。

## Stable CLI

public entry point は次の2本だけである。

- `baibai-engine <domain> <command>`: query と application service 経由の write
- `baibai-app`: local read-only UI（Baibai App）

主要 domain は `screening / macro / operation / position / proposal / research / task / db`。schema field、option、stdout YAML は public `--help` と engine modelを正とする。screening `run / select / ticker-profile` の YAML view は AI 向け安定契約であり、保存先が SQLite でも field の意味を変えない。

## Application data semantics

- canonical entity の作成・更新は DB transaction 内で current source と domain invariant を検証する。
- thesis / thesis review と holding review は immutable revision。source thesis revision への束縛を弱めない。
- proposal は `pending / approved / deferred / rejected` の current stateだけを持つ。broker factは人間報告後だけledger draftへ変換できる。
- ledger は append-only eventを `(occurred_at, same_instant_order)` でreplayする。既存event IDとlegacy decision referenceは保存し、新規eventを遡及挿入してcurrent snapshotを再計算できる。
- canonical ledger mutationは draft生成と、人間確認後の `position apply-draft --confirmed` を分離する。applyはexpected append head、proposal / reservation binding、置換対象rowを同一transactionで再検証する。
- operation sessionは6 kindの全体でactive最大1件。active rowのcurrent payloadを置換し、complete時に同じrowをimmutable final recordにする。checkpoint historyやtransition logは持たない。

## Read-only app invariants

`baibai-app` は `127.0.0.1` にだけbindし、write endpoint、migration、external network clientを持たない。application DB / run store / macro storeをSQLite read-only modeで開く。UIはDashboard、Screening、Shortlist、Security detail、Macroを提供し、proposal全state、operation active/completed、portfolio outcomeをquery-only viewで表示する。`/api/meta`はscreening / macro / application DBのas-of鮮度をstore内timestampから返し（file mtimeに依存しない）、UIは各画面のデータ鮮度として表示する。

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
views + machine history         Bearer認証 + static UI
```

- `baibai-stores` は `market.sqlite`、`runs.sqlite`、`macro.sqlite` のクラウド正本と、ローカル正本である`baibai.sqlite`のreplicaを保持する。public accessを持たない。
- `baibai-serving` は材料化済み`views/`と機械生成`history/`だけを保持する。`history/select/`は蓄積し、`history/candidates/`はR2 lifecycleで31日後に削除する。public accessを持たない。
- WorkerのR2 bindingは`baibai-serving`だけに限定する。`/api/*`は固定Bearer passwordをSHA-256後に定数時間比較し、有限のrouteから`views/` keyへ写像する。`history/`とstoresには到達しない。API応答は`Cache-Control: no-store`で、CORSを有効化しない。
- Workers Assetsは`ui/dist`を無認証で配信する。bundleは業務データを含まず、実データは認証済みAPIだけから取得する。HTTP navigationはWorkerが認証処理前にHTTPSへredirectし、HTTPS応答はHSTSを持つ。
- `cloud-materialize`はapplication dataの手動publishを材料化し、`cloud-daily-batch`は平日18:30 JSTに機械工程を実行する。両workflowは同じconcurrency groupでserving世代の混在を防ぐ。
- ローカル`pull`はmachine storeだけを置換し、canonical application DBを上書きしない。ローカル`publish`はSQLite snapshotをstoresへ置き、materializeをdispatchする。

具体的な初期構築、publish/pull、手動再実行、password rotationは[`tools/cloud/README.md`](../tools/cloud/README.md)を正本とする。

## Data layers

| layer | examples | rule |
| --- | --- | --- |
| L1 fact | market price、calendar、macro series | provider由来を保持し、再取得可能なstoreへ置く |
| L2 machine analysis | screening run、E[r]、FV anchor、machine selection | observed / derived / estimateを区別し、judgmentと呼ばない |
| L3 judgment / operation | macro context、shortlist、research、proposal、ledger、task、operation | application DBを正本にし、人間境界をwrite-timeに検証する |

fact / estimate / judgment の語彙と禁止事項は [`doctrine.md#fact-analysis-separation`](./doctrine.md#fact-analysis-separation) を正本とする。

## Development gates

機械契約はDB constraint、pydantic model、application service validationとnegative testで守る。通常のgateは `ruff format --check`、`ruff check`、`mypy`、`pytest`、import-linter、frontend buildである。設定fileはloader testで検証する。
