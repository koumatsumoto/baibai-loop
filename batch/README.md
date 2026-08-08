# Batch

## Purpose

request に起因しない production orchestration を所有する。

## Owns / Does not own

job sequencing、store transfer/merge、workflow input validation、watchdog、summary、notification を
所有する。投資 domain logic と Web projection logic は所有しない。

## Public entrypoints

GitHub Actions と ops skill 向けの内部入口は `baibai-batch`。operator shell は `scripts/`。

## Reads / Writes

engine/Web の安定入口を composeし、authority 規則に従って machine store と serving artifact を
転送する。application DB を cloud copy で上書きしない。

## Allowed / Forbidden dependencies

engine は `batch_api` / `read_api` の狭い境界だけを使う。web と tools、および engine domain
internal への直接依存は禁止する。

## Stores / Config / Reports

store authority と migration safety は [stores](../stores/README.md)、詳細な operator 手順は
[OPERATIONS](./OPERATIONS.md)。R2 object key topologyは変更しない。

## Tests

Python/shell/workflow contract は [`tests/batch`](../tests/batch)、依存と trust gate は
[`tests/contracts`](../tests/contracts)。

## Canonical docs

[architecture](../docs/architecture.md)、[OPERATIONS](./OPERATIONS.md)、
[ops-maintenance skill](../.agents/skills/ops-maintenance/SKILL.md)。

## Common change scenarios

scheduled job は `src/baibai_batch/jobs`、transfer/merge は `storage` と `scripts`、通知は
`observability`、入力・store validation は `validation` に置く。
