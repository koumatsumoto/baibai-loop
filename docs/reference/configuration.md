---
title: "Configuration reference"
summary: "Reference for runtime configuration, environment variables, and credentials boundaries."
doc_type: reference
status: active
last_reviewed: 2026-05-04
source_paths:
  - "../../src/baibai_loop/_env.py"
  - "../../src/baibai_loop/screening/config.py"
---

# Configuration

Configuration は runtime boundary です。secret や token の値は docs に書かず、必要な変数名と責務だけを記録します。

## Principles

- secret は repository に commit しない。
- `.env` の中身を docs や issue に貼らない。
- provider payload は config loader と normalizer の境界で検証する。
- CI secret は GitHub Actions secret として扱い、docs では名前と用途だけを説明する。

## Known runtime inputs

| input | 用途 | 主な利用箇所 |
| --- | --- | --- |
| `JQUANTS_REFRESH_TOKEN` | J-Quants API access / ledger sync | `.github/workflows/ledger-sync.yml`, screening / ledger provider |

実装上の strictness と validation boundary は [`python-foundation.md`](./python-foundation.md) を参照します。
