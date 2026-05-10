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
| `records/02-brief/` | `YYYY/MM/YYYY-MM-DD-{kind}-{slug}.yaml` |
| `records/03-outlook/` | `YYYY/MM/outlook-YYYY-MM-DD-<slug>.yaml` |
| `records/04-candidates/` | `YYYY/MM/YYYY-MM-DD.yaml` |
| `records/05-research/` | `YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md` |
| `records/06-trades/` | `YYYY/MM/YYYY-MM-DD-<ticker>.md` |
| `records/07-reviews/` | `YYYY/MM/YYYY-MM-DD-<ticker>.md` or `YYYY/retro-YYYYMM.md` |
| `records/_playbooks/` | `<playbook-slug>-v<n>.md` |

## Index And Reference Policy

Brief / outlook / candidates / research / trades / reviews は event records であり、
`_index.yaml` や `_changelog.jsonl` を持たない。正本は file path、record 内の repository
refs、CLI の `--asof` / explicit path で決まる。

Support area も content hash audit と changelog を持たない。判断時に参照した repo 内
file path を record に残し、変更履歴は git に一本化する。mutable latest index を足して
current state の解釈を二重化しない。

## Slugs

- 英小文字、数字、ハイフンを使う。
- 解釈語や評価語を避け、観測対象やイベント名を中立に表す。
- 小数点は `p` で代替する。例: `3.3%` -> `3p3`。

## Template links

Template 内の Markdown links は repository root からの絶対 path を使います。詳細は [`../templates/README.md`](../templates/README.md) を参照します。
