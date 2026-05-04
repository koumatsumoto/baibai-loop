---
title: "Docs style guide"
summary: "Style rules for front matter, shims, section anchors, and docs links."
doc_type: governance
status: active
last_reviewed: 2026-05-04
---

# Docs style guide

## Front matter

新規 docs と shim は最小 front matter を持ちます。

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
- `supersedes`
- `superseded_by`

## Shim format

旧 path を残す場合は、移行通知と旧本文を両方残します。1 行 redirect のみにはしません。

```md
---
title: "<old title>"
summary: "This document has moved, but the previous body is retained for compatibility."
doc_type: shim
status: superseded
last_reviewed: 2026-05-04
superseded_by: "docs/<new-path>.md"
---

> このファイルは `docs/<new-path>.md` へ移行しました。
> 既存リンクと節番号参照を守るため、当面は旧本文を残します。
> repo 全体のリンク張替えが完了し、旧 path 参照が消えたことを確認してから削除します。

<旧本文を残す>
```

## Section freeze

- `docs/components/*.md` は path と節構造を凍結します。
- `docs/philosophy.md`, `docs/design-principles.md`, `docs/anti-patterns.md` は現 path 維持です。
- 既存節番号や見出し名で参照される docs を split する場合は、旧 path に旧本文を残す shim を置きます。

## Links

- 新規 docs では現在の正本 path を参照します。
- 旧 path の shim は削除しません。
- `records/_playbooks/` など records 配下の Markdown は、GitHub で安定解決する repository root からの絶対 path を使ってよいです。
