---
title: "Workflow"
summary: "各工程がinputをoutputへ変換する方法と品質境界の入口。e2e順序はoperations、artifact契約はreferenceを参照する。"
doc_type: workflow-index
status: active
last_reviewed: 2026-07-20
---

# Workflow

workflowは「工程内でどう変換するか」を持つ。triggerと工程横断の順序は[`operations/decision-cycle.md`](../operations/decision-cycle.md)、artifact・式・modelの意味は[`reference/`](../reference/)を正本とする。

```mermaid
flowchart LR
  market[L1 market data] --> screening[L2 screening/select]
  screening --> research[L3 thesis]
  macro[material macro delta] -. context .-> research
  research --> proposal[trade proposal]
  proposal --> human[human broker action]
  human --> ledger[human-confirmed ledger]
  ledger --> holding[holding review/outcome]
  holding -. calibration .-> improve[improvement loop]
```

| 工程 | input | output | doc |
| --- | --- | --- | --- |
| macro | indicator series、一次source、refresh trigger | material delta contextまたは変更なし | [`macro.md`](./macro.md) |
| screening | point-in-time market/financial data、rules | screening run、longlist、machine selection | [`screening.md`](./screening.md) |
| research | shortlist、一次IR、ledger annotation | thesis、independent review、defer/reject | [`research.md`](./research.md) |
| position | human result、thesis/review、ledger、market close | ledger draft、holding review、outcome | [`position.md`](./position.md) |
| playbook | candidate evidence pattern | research checklist | [`playbooks.md`](./playbooks.md) |

AIはL2 estimateをfactと呼ばず、L3へ採用する値だけsource lineage付きで固定する。
