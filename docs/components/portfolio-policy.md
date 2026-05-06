---
title: "Portfolio policy"
summary: "Self-directed objectives, constraints, capital basis, risk budget, and execution guardrails upstream of briefs and screening."
doc_type: component
status: active
last_reviewed: 2026-05-06
related_docs:
  - "../concepts.md"
  - "../glossary/investment-terms-ja.md"
---

# Portfolio policy

Portfolio policy は、Baibai-Loop の判断ループより上流にある self-directed governance artifact です。目的、制約、資本、許容リスク、time horizon、eligible universe、liquidity constraints、kill switch を明文化し、research / trade が判断時点の policy assumptions を検証できるようにします。

Baibai-Loop は投資助言サービスではありません。Portfolio policy は他者に運用を委任する mandate ではなく、自分の裁量判断を後から検証するための統制文書です。

## 責務

- 投資判断の目的と constraints を明示する。
- Real capital、tactical real budget、paper proxy capital を分ける。
- 最大 concentration、liquidity cap、time stop、kill switch、eligible universe を定義する。
- Macro adverse の扱い、minimum payoff、risk/reward の下限など、research / trade が守る guardrail を定義する。
- 後続 artifact が使った policy assumptions を再現できるようにする。

## 対象外

- 個別銘柄 thesis の説明。
- Screening playbook の条件定義。
- Entry / target / stop / invalidation の個別設計。
- 実注文の約定記録。

これらは playbook、investment memo、execution record が扱います。

## Capital Concepts

| term | meaning |
| --- | --- |
| real capital | 実資金全体。実注文の最終的な concentration denominator になる |
| tactical real budget | 当面の取引に使ってよい実資金の上限。Real capital の一部として扱う |
| paper proxy capital | 判断の強弱を比較するための仮想資本。実注文額の分母ではない |

Paper proxy capital は、同じ thesis の強弱を比較するための proxy です。実際に何円注文するかは、policy の execution scaling、portfolio exposure、liquidity、board lot、price guard で決まります。

## Boundary With Playbooks

Portfolio policy は「この条件下でどの程度のリスクを許すか」を扱います。Playbook は「どの thesis pattern を見つけ、何を確認し、何が崩れたら無効化するか」を扱います。

| concern | source |
| --- | --- |
| eligible universe | portfolio policy |
| max concentration | portfolio policy |
| liquidity / ADV cap | portfolio policy |
| kill switch | portfolio policy |
| repeatable thesis pattern | playbook |
| evidence checklist | playbook / investment memo |
| target / stop / invalidation | investment memo |
| order submission / fill | trades |

## Snapshot Principle

Portfolio policy は governance component として扱います。現在の lifecycle では、capital / risk / liquidity の判断条件を research、ledger、trades の各 artifact に記録される field で監査します。後から policy が変わっても、当時の判断条件として記録された artifact field は書き換えません。
