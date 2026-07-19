---
title: "Operations index"
summary: "triggerから工程横断runbookを選ぶ入口。工程内の変換はworkflow、artifact contractはreferenceを参照する。"
doc_type: operation-index
status: active
last_reviewed: 2026-07-12
---

# Operations

operationは「いつ・どの順で完了するか」を持つ。各工程のinput/output/failureは[`workflow/`](../workflow/)、artifact・式・validationの意味は[`reference/`](../reference/)を正本とする。

| trigger / situation | runbook | 完了 |
| --- | --- | --- |
| 候補、指値、人間からの注文結果、月次入金、決算、保有、年次結果 | [`decision-cycle.md`](./decision-cycle.md) | trigger固有のproposal/draft/review/outcome |
| screening/FV/E[r]/macro読み等の方法変更 | [`improvement-loop.md`](./improvement-loop.md) | preregistration、評価、PR、運用テスト |
| 将来の決算・event後に再確認 | [`task-runbook.md`](./task-runbook.md) | application DBのopen taskからcanonical entityへ反映 |
| source/coverage/validator/CLI failure | [`incident-runbook.md`](./incident-runbook.md) | safe stop、復旧条件、escalation |

投資判断の正本はapplication DB、current workspaceとfinal resultはoperation sessionに置く。canonical entityはIDだけを参照し、同じ判断本文を複製しない。
