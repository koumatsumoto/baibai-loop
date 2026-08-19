# Historical evidence

## Purpose

methodology 改善の preregistration、結果、consumer contract を歴史的証拠として保存する。

## Owns / Does not own

`studies` は study 単位の evidence、`published` は明示的 consumer を持つ小さな machine-readable
surface、`operations` は不可逆または外部設定を伴う運用操作の実施記録（before / probe / after）を
所有する。production methodology と runtime state は所有しない。

## Public entrypoints

CLI は持たない。consumer は schema/method identity/source study を検証して `published` を読む。

## Reads / Writes

study は store/output を観測して記録するが canonical state を書かない。production method への採用は
別の明示 PR で行う。

## Allowed / Forbidden dependencies

report から runtime code を importせず、runtime から dated study file へ依存しない。

## Stores / Config / Reports

runtime state は [stores](../stores/README.md)、現在の規範は [method](../method/README.md)。study-local
data は各 study の `artifacts` に colocateする。

## Tests

published consumer と path/link contract は [`tests/web`](../tests/web) と
[`tests/contracts`](../tests/contracts)。

## Canonical docs

[doctrine](../docs/doctrine.md) と
[estimate calibration](../docs/reference/estimate-calibration.md)。

## Common change scenarios

新しい調査は `studies/YYYY-MM-DD-slug`、cross-subsystem consumer が必要な安定 artifact だけを
`published` に置く。クラウド設定のように後から差分を読めない操作は `operations/YYYY-MM-DD-slug`
へ実施記録を残す。
