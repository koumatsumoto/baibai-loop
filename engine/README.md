# Engine

## Purpose

Baibai Loop の authoritative business system。domain semantics と canonical write を所有する。

## Owns / Does not own

screening、macro、research、position、operation、proposal、task、application DB、query-only
`read_api` を所有する。Web presentation、schedule、store transfer、developer tooling は所有しない。

## Public entrypoints

公開入口は `baibai-engine` と `baibai_engine.read_api`。Batch 向けの狭い内部境界は
`baibai_engine.batch_api`。

## Reads / Writes

method と各 store を読み、application DB と domain-owned machine store を既存の write-time
validation 経由で書く。Web からの write は受け付けない。

## Allowed / Forbidden dependencies

engine 内の依存方向は [architecture](../docs/architecture.md) に従う。
`baibai_web`、`baibai_batch`、`tools` への依存は禁止し、import-linter で拒否する。

## Stores / Config / Reports

repository path の定義は `foundation.repository_layout`。authority は
[stores](../stores/README.md)、production rules は [method](../method/README.md)、evidence は
[reports](../reports/README.md) を参照する。

## Tests

Python contract は [`tests/engine`](../tests/engine)、cross-subsystem gate は
[`tests/contracts`](../tests/contracts)。

## Canonical docs

[doctrine](../docs/doctrine.md)、[architecture](../docs/architecture.md)、対象 domain の
[`docs/reference`](../docs/reference/README.md)、[AGENTS](../AGENTS.md)。

## Common change scenarios

domain behavior・writer・CLI は engine、query-only projection input は `read_api`、scheduled ordering
や retry は batch、表示都合は web に置く。
