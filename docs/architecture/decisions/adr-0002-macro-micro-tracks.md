---
title: "ADR 0002: Macro and micro tracks"
summary: "Record the decision to keep macro updates independent from the trade loop and integrate tracks in research."
doc_type: adr
status: accepted
last_reviewed: 2026-05-04
related_docs:
  - "../../philosophy.md"
  - "../information-flow.md"
---

# ADR 0002: Macro and micro tracks

## Status

Accepted

## Background

Macro events happen whether or not a trade is under consideration. Screening and research, on the other hand, only matter when the trading loop is active. Combining these flows makes stale macro context and ad hoc research selection more likely.

## Decision

Baibai-Loop keeps an independent macro track (`records/01-brief/` -> `records/02-outlook/`) and a trade-linked micro track (`records/03-candidates/` -> `records/04-research/` -> `records/05-trades/` -> `records/06-reviews/`). The integration point is `records/04-research/`.

## Rationale

This keeps macro freshness independent from trade cadence while preserving a clear gate before individual stock research. It also makes the macro 76% / micro 24% principle concrete: research cannot bypass outlook.
