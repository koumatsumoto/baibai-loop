# Web

## Purpose

canonical state を read-only で表示する browser-facing presentation system。

## Owns / Does not own

`backend` は FastAPI/read model/materialization、`frontend` は React、`edge` は認証付き serving、
`config` は表示設定、`contracts` は route/key inventory を所有する。domain logic と canonical
write、scheduled orchestration は所有しない。

## Public entrypoints

local UI は `baibai-web serve`、cloud serving は `web/edge`、materializer は
`python -m baibai_web.materialize`。

## Reads / Writes

engine state は `baibai_engine.read_api` だけから read-only で読む。materialized JSON と frontend
assets は生成するが、canonical store は書かない。

## Allowed / Forbidden dependencies

engine 依存は `read_api` に限定する。batch と tools への依存、engine internal writer への依存は
import-linter で拒否する。

## Stores / Config / Reports

store authority は [stores](../stores/README.md)。表示分類は `config/macro-panel.yaml`、consumer
契約を持つ evidence は `reports/published` から読む。R2 key は `contracts/routes.json` と既存
serving topologyを維持する。

## Tests

backend は [`tests/web`](../tests/web)、cross-runtime contract は
[`tests/contracts`](../tests/contracts)、frontend/edge は各 directory の `tests`。

## Canonical docs

[architecture](../docs/architecture.md) と
[Python foundation](../docs/reference/python-foundation.md)。

## Common change scenarios

API/read model/materializer は `backend`、画面は `frontend`、Bearer/HSTS/R2 mapping は `edge`、
表示設定は `config`、route drift 防止は `contracts` に置く。
