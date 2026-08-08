# Production methodology

## Purpose

現在 production で採用する Git-managed normative methodology を置く。

## Owns / Does not own

macro reading、screening rules、research playbooks を所有する。presentation config、runtime state、
historical evidence は所有しない。

## Public entrypoints

CLI は持たず、engine の loader が active revision を読む。

## Reads / Writes

runtime は read-only で参照する。変更は evidence を確認した明示 PR だけで行い、report から自動更新しない。

## Allowed / Forbidden dependencies

method は code を importしない。Web表示都合、cloud topology、runtime data を持ち込まない。

## Stores / Config / Reports

runtime state は [stores](../stores/README.md)、表示設定は [`web/config`](../web/config)、採否判断の
証拠は [reports](../reports/README.md)。

## Tests

loader/semantic contract は [`tests/engine`](../tests/engine)、path/drift は
[`tests/contracts`](../tests/contracts)。

## Canonical docs

[doctrine](../docs/doctrine.md) と domain 別 [`docs/reference`](../docs/reference/README.md)。

## Common change scenarios

機械読み規則は `macro/reading`、screening閾値は `screening/rules`、research手順は
`research/playbooks` に dated revision として置く。
