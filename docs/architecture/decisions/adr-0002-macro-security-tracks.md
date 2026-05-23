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

> 2026-05 更新: 旧 `brief -> outlook` track と macro gate fields は廃止し、`records/01-macro-context/` を screening / research の判断前提として使う。

## Status

Accepted

## Background

マクロイベントは売買検討の有無と独立に発生する。一方、screening と research は売買 loop が動くときに意味を持つ。両者を混ぜると、古い macro context のまま research を進めたり、その場限りの候補選定になったりしやすい。

## Decision

Baibai-Loop は、スクリーニング前に必要に応じて作成する `records/01-macro-context/` と、売買に連動する security-level trade loop (`records/04-candidates/` -> `records/05-research/` -> `records/06-trades/` -> `records/07-reviews/`) を分ける。統合点は screening selection と `records/05-research/` とする。

`records/04-candidates/` は機械スクリーニング結果を保存し、macro context との整合は `select` output と `records/05-research/` の `macro_context_fit` で確認する。

## Rationale

この分離により、macro を定期 record として増やし続けず、買い場探索に必要な局面でだけ判断前提を明文化できる。Macro context は hard gate ではなく、sector / exposure の優先度と避ける条件を示す運用 input として扱う。
