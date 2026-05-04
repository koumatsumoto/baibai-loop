---
title: "ADR 0001: Facts vs analysis separation"
summary: "Record the decision to physically separate factual artifacts from analysis artifacts."
doc_type: adr
status: accepted
last_reviewed: 2026-05-04
related_docs:
  - "../../philosophy.md"
  - "../../design-principles.md"
---

# ADR 0001: Facts vs analysis separation

## Status

Accepted

## Background

Baibai-Loop relies on AI-assisted drafting, but AI must not reuse past interpretation as if it were observed fact. If facts and interpretations live in the same artifact, later reviews cannot reliably distinguish fact error from analysis error.

## Decision

Factual artifacts and analysis artifacts are stored in separate directories. `records/01-brief/` and `records/03-candidates/` are fact layer. `records/02-outlook/` and `records/04-research/` are analysis layer.

## Rationale

Physical separation makes contamination visible in review and lets agents inspect fact inputs without loading previous analysis. This implements [`../../philosophy.md`](../../philosophy.md) pillar 1 and the operating rules in [`../../design-principles.md`](../../design-principles.md) §4.
