---
title: "Workflow — screening evidence patterns"
summary: "screening の型別 evidence と、個別 research で確認する人間向け checklist。"
doc_type: workflow
status: active
last_reviewed: 2026-07-20
---

# Workflow — Screening Evidence Patterns

`records/_playbooks/` は `playbook_id` ごとの research checklist を保持する。機械的な判定は行わず、`run` が出す `evidence_hits[]` と `select` の `selection_playbook` を受けて、個別調査で確認すべき論点を示す。

## Source Of Truth

- 閾値・除外条件・pattern の選定順: `records/_config/screening-rules/*.yaml`
- pattern ごとの research checklist: `records/_playbooks/<playbook_id>/YYYY-MM-DDTHHMMSS+0900.md`
- screening run / selection の公開 field: screening modelとpublic CLI YAML contract

Markdown 本文に固定の H2 構成を要求しない。decision packet は playbook 本文を参照せず、候補の source data と個別の判断を記録する。

## Change Rule

pattern を追加・変更・削除するときは、screening rules、対応する checklist、selection の順位・test を同じ変更で整合させる。閾値または型を変える根拠は estimate calibration の結果に置く。

## Current Patterns

運用中の一覧と個別 checklist は [`../../records/_playbooks/README.md`](../../records/_playbooks/README.md) を正とする。

## Reference

- [`./screening.md`](./screening.md): machine screen と evidence field
- [`./research.md`](./research.md): decision packet を作る個別調査
- [`../reference/estimate-calibration.md`](../reference/estimate-calibration.md): pattern 変更の計測経路
