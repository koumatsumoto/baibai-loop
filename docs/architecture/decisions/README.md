---
title: "Architecture decision records"
summary: "Index of accepted architecture decision records for Baibai-Loop."
doc_type: adr-index
status: active
last_reviewed: 2026-05-04
---

# Architecture decision records

ADR は GitHub issue や PR コメントに散らばりやすい設計判断を、あとから辿れる短い記録として残します。新規 ADR は既存 ADR の形式(背景 / 決定 / 影響)に合わせ、連番で追加します。

| ADR | Status | Decision |
| --- | --- | --- |
| [`adr-0001-facts-vs-analysis-separation.md`](./adr-0001-facts-vs-analysis-separation.md) | Accepted | fact layer と analysis layer を物理的に分離する |
| [`adr-0002-macro-security-tracks.md`](./adr-0002-macro-security-tracks.md) | Accepted | macro track と security-level trade loop を分け、research で統合する |
| [`adr-0003-playbooks-as-record-support-assets.md`](./adr-0003-playbooks-as-record-support-assets.md) | Accepted | playbook は docs ではなく `records/_playbooks/` の運用 asset とする |
