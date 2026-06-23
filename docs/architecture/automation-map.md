---
title: "Automation map"
summary: "Map of CLI commands, validators, schema files, tests, and CI."
doc_type: architecture
status: active
last_reviewed: 2026-05-04
source_paths:
  - "../../src/baibai_loop/"
  - "../../tests/"
  - "../../records/_schemas/"
  - "../../.github/workflows/"
---

# Automation map

Automation は人間の投資判断を置き換えるものではなく、fact snapshot の生成、schema 検証、ledger 正規化、CI 再現性を支える補助です。

## CLI

| command | 実装領域 | 責務 |
| --- | --- | --- |
| `uv run baibai-loop-screening bootstrap-cache --asof YYYY-MM-DD` | `src/baibai_loop/screening/` | screening run に必要な J-Quants / EDINET / JPX window を SQLite 正本へ補完する |
| `uv run baibai-loop-screening extract-edinet-metrics --asof YYYY-MM-DD` | `src/baibai_loop/screening/` | EDINET CSV から TTM metrics を抽出し SQLite 正本へ保存する |
| `uv run baibai-loop-screening verify-cache-coverage --asof YYYY-MM-DD` | `src/baibai_loop/screening/` | SQLite 正本が screening run の必須入力を read-only で満たすか検証する |
| `uv run baibai-loop-screening run --asof YYYY-MM-DD` | `src/baibai_loop/screening/` | 完全性検証済み SQLite 正本から candidates YAML を生成する |
| `uv run baibai-loop-screening select --asof YYYY-MM-DD --macro-context <path>` | `src/baibai_loop/screening/` | candidates と macro context を突合し、research 候補の ranking を支援する |
| `uv run baibai-loop-validation` | `src/baibai_loop/validate/` | records と schema の整合性を検証する |
| `uv run baibai-loop-position sync --root .` | `src/baibai_loop/position/` | decision event を `records/_ledger/` の JSONL に正規化する |

## Schema and validation

`records/_schemas/` は records validator の input schema を保持します。schema と validator の関係は [`../reference/testing-and-validation.md`](../reference/testing-and-validation.md) を正本とします。

| support area | 説明 |
| --- | --- |
| `records/_schemas/` | records artifact の validation schema |
| `src/baibai_loop/validate/` | schema だけでは表現しにくい cross-file validation |
| `tests/test_validate_*.py` | validator の期待挙動を固定する tests |

## CI

| workflow | trigger | gate |
| --- | --- | --- |
| `.github/workflows/ci.yml` | pull request / main push | `uv sync`, Ruff format/check, mypy, tracked raw screening cache block, pytest coverage, `baibai-loop-validation`, build |
| `.github/workflows/security.yml` | pull request / main push / weekly schedule | Bandit, pip-audit |

Python runtime、dependency、quality gate の詳細は [`../reference/python-foundation.md`](../reference/python-foundation.md) を参照します。
