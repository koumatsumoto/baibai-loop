---
title: "Testing and validation reference"
summary: "Reference for records schemas, validator boundaries, tests, and local verification commands."
doc_type: reference
status: active
last_reviewed: 2026-05-04
source_paths:
  - "../../records/_schemas/"
  - "../../src/baibai_loop/validation/"
  - "../../tests/"
---

# Testing and validation

`records/_schemas/` は records artifact の shape を固定する支援領域です。validator は schema だけでは表現しにくい cross-file rule と path existence を補完します。

YAML front matter の `ticker` は必ず quote する（`"9715"`）。unquoted は int として parse され、schema の `type: string` 違反で validator が error にする（先頭 0 落ちや `130A` のような英字入り code の破損防止）。

## Boundaries

| area | 責務 |
| --- | --- |
| `records/_schemas/` | YAML / front matter の schema 正本 |
| `src/baibai_loop/validation/` | schema validation と cross-file validation |
| `tests/test_validate_*.py` | validator の期待挙動 |
| `.github/workflows/ci.yml` | PR / main push の local parity gate |

## Local verification

records / schema / Python 実装を変更したら最低限以下を実行します。

```bash
uv run baibai-loop-validation
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

CI と完全に揃える場合は [`python-foundation.md`](./python-foundation.md) §9 を参照します。

## Docs path checks

docs 再編や link 更新をした場合は、少なくとも以下を確認します。

```bash
rg -nP '\]\((?!https?://|#|/)[^)]+\.md\)' docs README.md AGENTS.md
rg -n 'docs/' src tests records/_playbooks records/_schemas
```

relative Markdown links は GitHub 上で解決される path かを確認します。旧 path shim を削除する前には、repo 全体で旧 path の参照が消えていることを `rg` で確認します。
