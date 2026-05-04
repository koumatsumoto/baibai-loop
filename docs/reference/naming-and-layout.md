---
title: "Naming and layout reference"
summary: "Reference for records paths, file naming, template usage, and Markdown link path conventions."
doc_type: reference
status: active
last_reviewed: 2026-05-04
---

# Naming and layout

## Records layout

| path | naming pattern |
| --- | --- |
| `records/01-brief/` | `YYYY/MM/YYYY-MM-DD-{kind}-{slug}.yaml` |
| `records/02-outlook/` | `YYYY/MM/outlook-YYYY-MM-DD-<slug>.yaml` |
| `records/03-candidates/` | `YYYY/MM/YYYY-MM-DD.yaml` |
| `records/04-research/` | `YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md` |
| `records/05-trades/` | `YYYY/MM/YYYY-MM-DD-<ticker>.md` |
| `records/06-reviews/` | `YYYY/MM/YYYY-MM-DD-<ticker>.md` or `YYYY/retro-YYYYMM.md` |
| `records/_playbooks/` | `<playbook-slug>-v<n>.md` |

## Slugs

- 英小文字、数字、ハイフンを使う。
- 解釈語や評価語を避け、観測対象やイベント名を中立に表す。
- 小数点は `p` で代替する。例: `3.3%` -> `3p3`。

## Template links

Template 内の Markdown links は repository root からの絶対 path を使います。詳細は [`../templates/README.md`](../templates/README.md) を参照します。
