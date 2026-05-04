---
title: "ADR process"
summary: "Rules for numbering, writing, and updating Baibai-Loop architecture decision records."
doc_type: governance
status: active
last_reviewed: 2026-05-04
---

# ADR process

ADR は [`../architecture/decisions/`](../architecture/decisions/) に置きます。

## Numbering

- File name は `adr-0001-short-slug.md` 形式の連番にします。
- 日付 prefix は使いません。
- supersede する場合は新しい ADR を追加し、古い ADR の status と `superseded_by` を更新します。

## Required sections

- `Status`
- `Background`
- `Decision`
- `Rationale`

## Language

- ADR の `title`, `summary`, 本文は日本語で書きます。
- `Status`, `Background`, `Decision`, `Rationale` の見出しと status 値は、検索しやすさを優先して英語の識別子を使います。

## Status

- `Proposed`
- `Accepted`
- `Superseded`
- `Rejected`

初期 seed ADR は `Status: Accepted` とし、背景、決定、根拠を 1-2 段落ずつに留めます。

## Index update

新しい ADR を追加したら、同じ commit で [`../architecture/decisions/README.md`](../architecture/decisions/README.md) の table も更新します。
