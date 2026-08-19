# Runtime stores

## Purpose

実行時の永続 state を authority と rebuildability が見える責務別 path に置く。実データは Git 管理しない。

## Owns / Does not own

canonical application state と machine store を所有する。production methodology、historical evidence、
一時 cache は所有せず、それぞれ `method`、`reports`、`.cache` に置く。

## Public entrypoints

store 操作は `baibai-engine` の domain CLI と [`batch/scripts`](../batch/scripts) を使う。
`baibai-engine lake inventory` はlocal R2 mirrorのfile metadataだけを読み、`lake validate`は
manifest contractだけを検査する。どちらもobjectやpointerを書き換えない。`lake resolve`は
current pointerを1度だけ解決し、`lake hydrate`はその固定releaseから`market.sqlite`のlake所有
15 tableを満たす。詳細手順は [Batch operations](../batch/OPERATIONS.md) と
[market lake](../docs/reference/market-lake.md)。

## Reads / Writes

| store | authority | writer | backup / rebuild | cloud sync |
| --- | --- | --- | --- | --- |
| `application/baibai.sqlite` | local canonical、cloud replica | engine application service | `baibai-engine db backup`。自動 rebuild 禁止 | `batch/scripts/publish.sh` |
| `market/market.sqlite` | fetch由来15 tableはR2のL1 releaseから再構築されるruntime copy、残る4 tableはここがcanonical | provider + controlled merge | releaseからhydrate、または screening cache command で再取得可能 | lake所有15 tableを空にしてから push |
| R2 `lake/l1/` | fetch由来15 datasetのcanonical L1 | lake publisher | source再取得またはlegacy SQLite seedからimmutable rebuild | content object + manifest + CAS pointer |
| `lake/` | disposable local R2 mirror / staging / content-addressed object cache | lake build | R2 manifestから再取得可能 | authorityにしない |
| `macro/macro.sqlite` | cloud rolling + local full history | macro indicator service + controlled merge | provider series から再取得可能 | no-loss merge 後のみ push |
| `screening/runs.sqlite` | cloud canonical | daily batch screening service | screening run から再生成可能 | local から push 禁止 |
| `screening/calibration/` | rebuildable L2（typed Parquet + atomic calibration bundle pointer） | engine calibration command | market/ledger evidence またはdigest固定したlegacy archiveから再生成可能 | 3 dataset manifestをbundleとしてpublish |

## Allowed / Forbidden dependencies

writer は上表の owner に限定する。市場 fact の authority は lake にあり、旧 `data/` path、新旧同時
canonical writer、application DB の自動初期化、cloud copyによるlocal canonical上書きを禁止する。
上表の path はすべて repository root からの相対で、runtime は起動前に root を確認し、root 以外
（`engine/` などの部分木）からの起動は store を作らずに停止する。
`market.sqlite` の lake 所有 15 table は fixed release から再構築できる runtime copy であり、
R2 が持つ copy はその 15 table を空にしたものになる（[market lake](../docs/reference/market-lake.md#daily-cutover)）。

## Stores / Config / Reports

production rules は [method](../method/README.md)、historical evidence は
[reports](../reports/README.md)。R2 object keyは`lake/`以下のpath-safe segmentだけで構成し、
dataset / release manifestがobject inventory、checksum、rows、coverage、producerを固定する。
manifestのlogical identityはcontent SHA-256で固定し、R2 ETagはpointer CAS等のtransport stateに
限定する。sourceはtyped `SourceRef`、release completenessはmanifestが宣言したprofileのpolicyで検証する。
repository path migration とR2 key semanticsを結合しない。

## Tests

schema/write invariants は [`tests/engine`](../tests/engine)、transfer/merge は
[`tests/batch`](../tests/batch)、path gate は [`tests/contracts`](../tests/contracts)。

## Canonical docs

[architecture](../docs/architecture.md)、[portfolio ledger](../docs/reference/portfolio-ledger.md)、
[screening runtime](../docs/reference/screening-runtime.md)、[Batch operations](../batch/OPERATIONS.md)。

## Common change scenarios

schema変更は対応codeをmainへ入れてからcloudへ反映する。移行時は backup、`quick_check`、schema、
required tables、canonical row/head/ledger identity を確認し、旧pathが残る状態ではruntimeを起動しない。
layoutのforward/rollbackは[Batch operations](../batch/OPERATIONS.md#repository-store-layout-の-cutover-と-rollback)
のdry-run付きone-time commandを使い、codeだけを先に切り替えない。
