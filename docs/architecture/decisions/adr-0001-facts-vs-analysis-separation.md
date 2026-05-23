---
title: "ADR 0001: 事実と分析の物理分離"
summary: "事実成果物と分析成果物を別 directory に分離する判断を記録する。"
doc_type: adr
status: accepted
last_reviewed: 2026-05-04
related_docs:
  - "../../philosophy.md"
  - "../../design-principles.md"
---

# ADR 0001: 事実と分析の物理分離

> 2026-05 更新: 旧 `brief` / `outlook` は廃止し、macro の事実確認と方向性判断を `records/01-macro-context/` に統合した。

## Status

Accepted

## Background

Baibai-Loop は AI 下書きを前提にするが、AI が過去の解釈を観測事実として再利用してはならない。事実と解釈が同じ成果物に混在すると、後続 review で「事実誤認」か「分析誤り」かを切り分けにくくなる。

## Decision

事実成果物と分析成果物を別 directory に保存する。`records/04-candidates/` は screening fact layer、`records/01-macro-context/` と `records/05-research/` は analysis layer とする。

## Rationale

物理分離により、analysis の混入を review で発見しやすくし、agent が過去分析を読み込まずに fact input だけを確認できる。これは [`../../philosophy.md`](../../philosophy.md) の柱 1 と [`../../design-principles.md`](../../design-principles.md) §4 の運用ルールを具体化する。
