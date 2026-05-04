---
title: "ADR 0003: Playbooks as record support assets"
summary: "Record the decision to keep active playbooks under records/_playbooks instead of docs."
doc_type: adr
status: accepted
last_reviewed: 2026-05-04
related_docs:
  - "../../components/playbooks.md"
  - "../../../records/_playbooks/README.md"
---

# ADR 0003: Playbooks as record support assets

## Status

Accepted

## Background

Playbooks are not background documentation. They are active operating rules referenced from research front matter and revised through reviews. Treating them as docs would blur the boundary between explanatory material and live decision assets.

## Decision

Active playbooks live under `records/_playbooks/`. `docs/components/playbooks.md` documents their contract, lifecycle, and relationship with research and reviews.

## Rationale

Keeping playbooks in `records/` makes them part of the forward-only operating evidence while still allowing docs to explain how they are used. This also aligns playbook revisions with monthly retro rather than docs-only maintenance.
