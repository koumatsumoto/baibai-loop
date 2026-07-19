---
title: "Testing and validation"
summary: "DB constraint、engine model、application service、config loaderの検証境界。"
doc_type: reference
status: active
last_reviewed: 2026-07-19
---

# Testing and validation

application dataの機械契約は、次の層でwrite時に検証する。

| layer | responsibility |
| --- | --- |
| SQLite constraint / trigger | required identity、enum、foreign key、immutable row、active最大1件 |
| pydantic / domain model | field type、shape、cross-field invariant |
| application service | current source、revision、proposal / reservation、人間確認、stale no-write |
| config loader | Git管理のscreening rules、Macro dashboard config、playbookの構造 |

schema fileやlive YAML treeを横断するvalidator CLIは置かない。保持すべきruleは各write pathのnegative testで反証し、file/path固有のruleはapplication dataをfileに保存しないため適用しない。

## Required gates

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run lint-imports
cd ui && npm run build
```

高影響のDB変更では正常系だけでなく、conflicting ID、invalid enum、missing reference、revision drift、stale draft、人間確認なし、read-only appからのwrite不能をtestする。ledgerはevent順序、snapshot全field、market price / override / metaをfixtureと比較する。

public CLIのoptionは`--help`、YAML stdoutはmodel / contract test、UI queryはread-only source contract testを正とする。docsはfield定義を複写せず、意味、境界、WHYを記載する。
