# Runtime stores

## Purpose

実行時の永続 state を authority と rebuildability が見える責務別 path に置く。実データは Git 管理しない。

## Owns / Does not own

canonical application state と machine store を所有する。production methodology、historical evidence、
一時 cache は所有せず、それぞれ `method`、`reports`、`.cache` に置く。

## Public entrypoints

store 操作は `baibai-engine` の domain CLI と [`batch/scripts`](../batch/scripts) を使う。詳細手順は
[Batch operations](../batch/OPERATIONS.md)。

## Reads / Writes

| store | authority | writer | backup / rebuild | cloud sync |
| --- | --- | --- | --- | --- |
| `application/baibai.sqlite` | local canonical、cloud replica | engine application service | `baibai-engine db backup`。自動 rebuild 禁止 | `batch/scripts/publish.sh` |
| `market/market.sqlite` | cloud daily + local deep history | provider + controlled merge | screening cache command で再取得可能 | no-loss merge 後のみ push |
| `macro/macro.sqlite` | cloud rolling + local full history | macro indicator service + controlled merge | provider series から再取得可能 | no-loss merge 後のみ push |
| `screening/runs.sqlite` | cloud canonical | daily batch screening service | screening run から再生成可能 | local から push 禁止 |
| `screening/calibration/` | rebuildable L2 | engine calibration command | market/ledger evidence から再生成可能 | production store upload対象外 |

## Allowed / Forbidden dependencies

writer は上表の owner に限定する。旧 `data/` path、新旧同時writer、application DB の自動初期化、
cloud copyによるlocal canonical上書きを禁止する。

## Stores / Config / Reports

production rules は [method](../method/README.md)、historical evidence は
[reports](../reports/README.md)。R2 object key semantics は repository path migration と独立して維持する。

## Tests

schema/write invariants は [`tests/engine`](../tests/engine)、transfer/merge は
[`tests/batch`](../tests/batch)、path gate は [`tests/contracts`](../tests/contracts)。

## Canonical docs

[architecture](../docs/architecture.md)、[portfolio ledger](../docs/reference/portfolio-ledger.md)、
[screening runtime](../docs/reference/screening-runtime.md)、[Batch operations](../batch/OPERATIONS.md)。

## Common change scenarios

schema変更は対応codeをmainへ入れてからcloudへ反映する。移行時は backup、`quick_check`、schema、
required tables、canonical row/head/ledger identity を確認し、旧pathが残る状態ではruntimeを起動しない。
