# Developer tools

## Purpose

repository を開発・検証するための非 production tooling。

## Owns / Does not own

quality gate、experiment、generator、diagnostic を所有する。production runtime、domain semantics、
scheduled operations は所有しない。

## Public entrypoints

stable public CLI は持たない。各 tool は repository-local module/script として明示的に実行する。

## Reads / Writes

必要に応じて engine/web/batch output を読む。生成物は明示された asset/report だけへ書き、runtime
store の canonical writer にならない。

## Allowed / Forbidden dependencies

tools から runtime package への依存は必要最小限で許容する。engine/web/batch から tools への依存は
禁止する。

## Stores / Config / Reports

runtime store は所有しない。experiment evidence は [reports](../reports/README.md)、production rule は
[method](../method/README.md) に明示的な採用 PR で反映する。

## Tests

[`tests/tools`](../tests/tools) と [`tests/contracts`](../tests/contracts)。

## Canonical docs

[architecture](../docs/architecture.md) と
[Python foundation](../docs/reference/python-foundation.md)。

## Common change scenarios

CI/drift は `quality`、一回または改善計測は `experiments`、asset作成は `generators`、調査補助は
`diagnostics` に置く。
