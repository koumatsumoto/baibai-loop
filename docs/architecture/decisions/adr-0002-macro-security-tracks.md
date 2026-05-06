---
title: "ADR 0002: macro track と security-level trade loop の分離"
summary: "macro 更新を売買 loop から独立させ、research で統合する判断を記録する。"
doc_type: adr
status: accepted
last_reviewed: 2026-05-04
related_docs:
  - "../../philosophy.md"
  - "../information-flow.md"
---

# ADR 0002: macro track と security-level trade loop の分離

## Status

Accepted

## Background

マクロイベントは売買検討の有無と独立に発生する。一方、screening と research は売買 loop が動くときに意味を持つ。両者を混ぜると、古い macro context のまま research を進めたり、その場限りの候補選定になったりしやすい。

## Decision

Baibai-Loop は独立した macro track (`records/02-brief/` -> `records/03-outlook/`) と、売買に連動する security-level trade loop (`records/04-candidates/` -> `records/05-research/` -> `records/06-trades/` -> `records/07-reviews/`) を分ける。統合点は `records/05-research/` とする。

## Rationale

この分離により、macro の鮮度を trade cadence から独立して維持しつつ、個別銘柄 research の前に明確な gate を置ける。Macro 76% / security-level 24% は attention / review time / cognitive budget の policy weight として扱い、research は outlook を bypass できない。
