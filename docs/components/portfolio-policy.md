---
title: "Portfolio policy"
summary: "Self-directed objectives, constraints, capital basis, risk budget, and execution guardrails upstream of macro context and screening."
doc_type: component
status: active
last_reviewed: 2026-05-06
related_docs:
  - "../concepts.md"
---

# Portfolio policy

Portfolio policy は、Baibai-Loop の判断ループより上流にある self-directed governance artifact です。目的、制約、資本、許容リスク、time horizon、eligible universe、liquidity constraints、kill switch を明文化し、research / trade が判断時点の policy assumptions を検証できるようにします。

Baibai-Loop は投資助言サービスではありません。Portfolio policy は自分の裁量判断を後から検証するための統制文書です。

## 責務

- 投資判断の目的と constraints を明示する。
- Real capital、tactical real budget、paper proxy capital を分ける。
- 最大 concentration、liquidity cap、time stop、kill switch、eligible universe を定義する。
- Macro context headwind の扱い、minimum payoff、risk/reward の下限など、research / trade が守る guardrail を定義する。
- Swing-first / long-hold-capable value strategy を定義し、短期利確と長期保有 fallback の境界を明示する。
- 後続 artifact が使った policy assumptions を再現できるようにする。

## 対象外

- 個別銘柄 thesis の説明。
- Screening playbook の条件定義。
- Entry / target / stop / invalidation の個別設計。
- 実注文の約定記録。

これらは playbook、investment memo、execution record が扱います。

## Strategy Principles

### Swing-first / long-hold-capable value principle

Baibai-Loop の主戦略は、5-40 営業日の中期スイングトレードで一時的に過小評価された銘柄を買い、短期から中期で価格回復・catalyst・需給改善が出た場合に利確することである。

ただし、想定通りに上昇せず売却タイミングを逃した場合でも、長期保有へ切り替えられる銘柄を優先する。そのため、採用候補は valuation の割安さだけでなく、長期保有になっても耐えられる可能性が高い balance sheet、cash flow、流動性、借換リスク、収益基盤を確認する。ここでいう長期保有は固定年数の条件ではなく、売却までの期間が想定より長引いても事業継続性と回収余地が残るかを見るための selection principle である。

含み損が出ている場合は、損失確定を急がず長期保有へ切り替える余地を持つ。その間の資産ロックは受け入れる。ただし、この方針は短期 thesis が外れた場合に損失を無視するためのものではない。長期保有へ切り替える余地がある銘柄だけを最初から選び、資本毀損・機会損失・thesis 破綻のリスクを下げるための selection principle である。

Long-hold fallback は stop loss、invalidation、kill switch、事業継続前提の毀損を上書きしない。長期保有へ切り替えるのは、短期の価格回復 timing を逃しただけで、事業継続性、cash flow、balance sheet、thesis の中核が維持されている場合に限る。

資産ロック中にも収益が見込めるため、配当、自己株買い、安定した shareholder return がある銘柄は優先する。ただし、配当のないお買い得銘柄でも、短期リターンの可能性と payoff が十分に大きく、balance sheet / cash flow の耐久性が確認できる場合は、リスクを取って採用してよい。

### AI long-term structural impact principle

AI は長期では産業規模、需要構造、コスト構造、競争優位、顧客 capex、disruption risk を変え得る構造テーマとして扱う。Baibai-Loop では、AI 影響を短期のテーマ買いではなく、long-hold fallback の質を評価する strategic lens として確認する。

AI が長期追い風になり得る場合は、下落時に長期保有へ切り替える選択肢の期待値を高める可能性がある。一方で、AI による既存事業の disruption、顧客投資循環の鈍化、valuation 過熱、競争優位の毀損は long-hold fallback を弱める要因として扱う。

AI 期待は単独の採用根拠、position sizing 根拠、macro context fit、validator-visible rule にはしない。採用判断は valuation、cash flow、balance sheet、catalyst、競争優位、資本配分、決算鮮度と合わせて行う。

### Policy field との境界

上記 2 原則は strategic attention principle であり、validator-visible な hard gate / cap / sizing rule ではない。機械検証は capital、risk、liquidity、minimum payoff、macro context fit などの構造 field に限定し、長期保有 fallback と AI 長期影響の確認は research / macro context の template、runbook、self-review で担保する。

## Capital Concepts

| term | meaning |
| --- | --- |
| real capital | 実資金全体。実注文の最終的な concentration denominator になる |
| tactical real budget | 当面の取引に使ってよい実資金の上限。Real capital の一部として扱う |
| paper proxy capital | 判断の強弱を比較するための仮想資本。実注文額の分母ではない |

Paper proxy capital は、同じ thesis の強弱を比較するための proxy です。実際に何円注文するかは、policy の execution scaling、liquidity、board lot、price guard で決まります。

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

## Policy Reference Principle

Portfolio policy は governance component として扱います。現在の lifecycle では、capital / risk / liquidity の具体的な閾値は code-managed policy config で管理し、この document は判断方針と背景を説明します。履歴は git で確認します。過去背景を policy docs に残さず、現行 policy の理解しやすさを優先します。
