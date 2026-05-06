---
title: "Docs review process"
summary: "Review and verification process for docs changes, links, and PR descriptions."
doc_type: governance
status: active
last_reviewed: 2026-05-04
---

# Docs review process

## Review checklist

- root `README.md` が詳細規範を持ちすぎていないか。
- `docs/README.md` から主要 docs に辿れるか。
- component docs の path と節構造を壊していないか。
- 移動履歴や互換用 redirect/shim を本文に残していないか。
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

## Removed path check

docs の移動・統合・削除を行う場合は、少なくとも以下を実行します。

```bash
rg -n 'docs/<old-path>.md|<old-heading-text>' README.md AGENTS.md docs src tests records/_playbooks records/_schemas
rg -n '\]\((\.{1,2}/)?(architecture|workflow|data-sources|python-foundation)\.md' docs
```

内部参照が残る場合は、同じ PR で正本 path に更新します。

## PR body

計画 issue 全文を PR description に貼りません。該当 section だけを要約し、変更内容、検証結果、受け入れ済みリスクを短く書きます。
