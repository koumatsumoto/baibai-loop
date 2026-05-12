---
title: "Research runbook"
summary: "Operational entry point for creating records/05-research packets from candidates and outlook."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/research.md"
  - "../components/candidates.md"
  - "../components/outlook.md"
  - "./screening-runbook.md"
  - "./task-runbook.md"
---

# Research runbook

Research は `records/04-candidates/` と `records/03-outlook/` を統合する analysis layer です。packet の contract、front matter、AI 境界、self-review は [`../components/research.md`](../components/research.md) を正本とします。

## Before writing

1. 最新 candidates と最新 outlook が存在することを確認する。
2. Macro regime gate は [`screening-runbook.md`](./screening-runbook.md) と [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md) に従う。
3. 使用する playbook が [`../components/playbooks.md`](../components/playbooks.md) と `records/_playbooks/` から辿れることを確認する。
4. Policy の swing-first / long-hold-capable value principle を確認し、短期 thesis が外れた場合でも 5 年程度の長期保有へ切り替えられる候補かを確認する。
5. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-09 を全体 gate として確認する。特に AP-01, AP-02, AP-03, AP-04, AP-06, AP-08, AP-09 は research で重点確認する。

## Rules

- `candidate_ref.candidates_ref` と `outlook_ref` を必ず実在 path にする。`candidate_ref` は `candidates_ref` / `screen_run_id` / `ticker` / `candidate_id` の完全 join key として candidates row に一致させる。
- Macro regime gate を通らない銘柄を採用しない。
- 銘柄固有の事実は、業種を問わず会社IRを一次情報として確認し、出典と計算根拠を残す。直近決算短信、
  決算説明資料、Q&A、有価証券報告書 / 統合報告書、中期経営計画、株主還元関連開示を未確認のまま
  `research_decision.outcome: approved` にしない。
- Thesis には long-hold fallback を 1 行以上書く。5 年程度の balance sheet / cash flow / liquidity / refinancing risk / earnings base の耐久性、資産ロック許容、配当・自己株買いなどの shareholder return を確認する。配当がない銘柄は、短期リターン可能性と payoff が大きい場合だけ採用余地を残す。
- Thesis には AI long-term impact を 1 行以上書く。AI の長期機会・長期脅威・今回判断での重みを明示し、AI 期待だけで採用や sizing を正当化しない。
- 採用判定は `research_decision.outcome` と `research_decision.posture` の意味を [`../components/research.md`](../components/research.md) に合わせる。
- `research_decision.outcome: deferred` かつ `research_decision.posture: wait_for_event` の場合は、[`task-runbook.md`](./task-runbook.md) に従い、決算後確認タスク issue を作成または既存 issue に紐づける。
- 訂正が必要な場合は既存行を書き換えず、decision register に correction event を追加する。

## After writing

```bash
uv run baibai-loop-validate
uv run baibai-loop-ledger sync --root .
```
