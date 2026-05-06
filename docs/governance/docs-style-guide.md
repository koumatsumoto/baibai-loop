---
title: "Docs style guide"
summary: "Style rules for front matter, section anchors, and docs links."
doc_type: governance
status: active
last_reviewed: 2026-05-04
---

# Docs style guide

## Front matter

新規 docs は最小 front matter を持ちます。

```yaml
---
title: "<title>"
summary: "<one sentence summary>"
doc_type: "<type>"
status: active
last_reviewed: 2026-05-04
---
```

Optional fields:

- `owner`
- `reviewers`
- `review_cycle_days`
- `source_paths`
- `related_docs`
- `related_issues`

## Section freeze

- `docs/components/*.md` は path と節構造を凍結します。
- `docs/philosophy.md`, `docs/design-principles.md`, `docs/anti-patterns.md` は現 path 維持です。
- 節番号や見出し名で参照される docs を split する場合は、参照元を同じ PR で正本 path に更新します。

## Links

- 新規 docs では現在の正本 path を参照します。
- 本文には移動履歴や互換用の redirect/shim を残しません。
- `records/_playbooks/` など records 配下の Markdown は、GitHub で安定解決する repository root からの絶対 path を使ってよいです。
