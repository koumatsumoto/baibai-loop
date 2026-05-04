---
title: "Docs review process"
summary: "Review and verification process for docs changes, shims, links, and PR descriptions."
doc_type: governance
status: active
last_reviewed: 2026-05-04
---

# Docs review process

## Review checklist

- root `README.md` が詳細規範を持ちすぎていないか。
- `docs/README.md` から主要 docs に辿れるか。
- component docs の path と節構造を壊していないか。
- shim は [`docs-style-guide.md`](./docs-style-guide.md) の形式に従っているか。
- retained canonical docs (`philosophy.md`, `design-principles.md`, `anti-patterns.md`) を不要に移動していないか。
- `records/_*` の責務が [`../architecture/repository-map.md`](../architecture/repository-map.md) から辿れるか。

## Verification

```bash
rg -n 'view_ref|--view|\bview\b|screened|architecture-v1|root playbooks' README.md AGENTS.md docs
rg -nP '\]\((?!https?://|#|/)[^)]+\.md\)' docs README.md AGENTS.md
rg -n 'records/02-outlook|records/03-candidates|records/_playbooks' docs README.md AGENTS.md
rg -n 'docs/' src tests records/_playbooks records/_schemas
```

PCRE2 syntax を使う grep には `-P` を付けます。

## Shim deletion

shim を削除する前に、少なくとも以下を実行します。

```bash
rg -n 'docs/<old-path>.md|<old-heading-text>' README.md AGENTS.md docs src tests records/_playbooks records/_schemas
```

内部参照が消えていない場合は削除しません。外部 issue / PR / permalink からの参照は残るため、削除は個別 issue で判断します。

## PR body

計画 issue 全文を PR description に貼りません。該当 section だけを要約し、変更内容、残した shim、検証結果、受け入れ済みリスクを短く書きます。
