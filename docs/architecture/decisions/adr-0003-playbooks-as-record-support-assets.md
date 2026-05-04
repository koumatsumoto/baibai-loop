---
title: "ADR 0003: playbook を records support asset として扱う"
summary: "active playbook を docs ではなく records/_playbooks に置く判断を記録する。"
doc_type: adr
status: accepted
last_reviewed: 2026-05-04
related_docs:
  - "../../components/playbooks.md"
  - "../../../records/_playbooks/README.md"
---

# ADR 0003: playbook を records support asset として扱う

## Status

Accepted

## Background

Playbook は背景説明の docs ではない。research front matter から参照され、reviews を通じて改訂される active operating rule である。docs として扱うと、説明資料と実際の判断 asset の境界が曖昧になる。

## Decision

active playbook は `records/_playbooks/` に置く。`docs/components/playbooks.md` は、その contract、lifecycle、research / reviews との関係を説明する。

## Rationale

Playbook を `records/` に置くことで、forward-only な運用証跡の一部として扱える。一方で docs は使い方を説明できる。これにより、playbook 改訂は docs maintenance ではなく monthly retro と結び付く。
