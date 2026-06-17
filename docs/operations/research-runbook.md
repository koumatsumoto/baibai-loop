---
title: "Research runbook"
summary: "Operational entry point for creating records/05-research packets from candidates and macro context."
doc_type: operation
status: active
last_reviewed: 2026-05-04
related_docs:
  - "../components/research.md"
  - "../components/candidates.md"
  - "../components/macro-context.md"
  - "./screening-runbook.md"
  - "./task-runbook.md"
---

# Research runbook

Research は `records/04-candidates/` と `records/01-macro-context/` を統合する analysis layer です。packet の contract、front matter、AI 境界、self-review は [`../components/research.md`](../components/research.md) を正本とします。

## Before writing

1. 最新 candidates と最新 macro context が存在することを確認する。
2. [`screening-runbook.md`](./screening-runbook.md) の select 手順に従い、`select` の recommendations から着手候補を選ぶ。閾値を試したい場合だけ `select-sweep --profile-config` で custom profile を比較する。
3. Macro context は hard gate ではなく、候補の thesis / risk / sector 前提を確認する入力として使う。
4. 使用する playbook が [`../components/playbooks.md`](../components/playbooks.md) と `records/_playbooks/` から辿れることを確認する。
5. Policy の swing-first / long-hold-capable value principle を確認し、短期 thesis が外れた場合でも長期保有へ切り替えられる候補かを確認する。
6. [`../anti-patterns.md`](../anti-patterns.md) の AP-01〜AP-09 を全体 gate として確認する。特に AP-01, AP-02, AP-03, AP-04, AP-06, AP-08, AP-09 は research で重点確認する。

## Rules

- `candidate_ref.candidates_ref` と `macro_context_ref` を必ず実在 path にする。`candidate_ref` は `candidates_ref` / `ticker` で candidates row に一致させる。
- Macro context が headwind の場合も自動却下せず、sizing caution や required checks として扱う。
- 銘柄固有の事実は、業種を問わず会社IRを一次情報として確認し、出典と計算根拠を残す。直近決算短信、
  決算説明資料、Q&A、有価証券報告書 / 統合報告書、中期経営計画、株主還元関連開示を未確認のまま
  `research_decision.outcome: approved` にしない。
- Thesis には long-hold fallback を 1 行以上書く。長期保有になっても耐えられる可能性が高い balance sheet / cash flow / liquidity / refinancing risk / earnings base の耐久性、資産ロック許容、配当・自己株買いなどの shareholder return を確認する。固定年数の条件ではなく、売却までの期間が想定より長引いても事業継続性と回収余地が残るかを確認する。配当がない銘柄は、短期リターン可能性と payoff が大きい場合だけ採用余地を残す。Long-hold fallback は stop loss、invalidation、kill switch、事業継続前提の毀損を上書きしない。
- Thesis には AI long-term impact を 1 行以上書く。AI の長期機会・長期脅威・今回判断での重みを明示し、AI 期待だけで採用や sizing を正当化しない。
- 採用判定は `research_decision.outcome` と `research_decision.posture` の意味を [`../components/research.md`](../components/research.md) に合わせる。
- `research_decision.outcome: deferred` かつ `research_decision.posture: wait_for_event` の場合は、[`task-runbook.md`](./task-runbook.md) に従い、決算後確認タスク issue を作成または既存 issue に紐づける。
- 訂正が必要な場合は既存行を書き換えず、decision register に correction event を追加する。

## Filling entry_preflight.market_regime

2026-06-17 以降の `approved` research は `entry_preflight.market_regime.regime` が必須。値は `screening/regime.py` の判定（benchmark 20bd return の閾値 ±3%）と一致させる。

paste-ready の方法:

```bash
# 個別銘柄の packet（regime + relative + events）を取得
uv run baibai-loop-screening ticker-profile --ticker 4432 --asof 2026-06-17

# 市場全体の regime のみ（複数候補をまとめて評価する場合）
uv run baibai-loop-screening market-snapshot --asof 2026-06-17
```

出力の `market_regime` block を front matter にコピーし、最低限以下を残す:

```yaml
market_regime:
  regime: risk_on_rally        # or risk_off_selloff / neutral_range / unknown
  benchmark_return_20d: 0.0748  # ratio (not %)
  benchmark_ticker: "1321"
  asof: "2026-06-17"
  eval_date: "2026-06-17"
```

failure mode と validator の挙動:

- `data/screening/market.sqlite` が無い fresh checkout では `ticker-profile` が空 regime を返す。`uv run baibai-loop-screening bootstrap-cache --asof YYYY-MM-DD` を先に流す。
- `regime: unknown` で書くと proceed が hard-block される（unknown は「判定できない」を意味し proceed の根拠にはならない）。bootstrap-cache を流すか `action: defer` を選ぶ。
- `regime: risk_on_rally` のとき:
  - `action: proceed` は hard-block（error）
  - `action: starter` で `near_term_catalyst: true` も `exception_basis: [low_correlation]` も無い場合は warning（規律 nudge）
  - `action: exception` で同条件は error（waiver 基拠を明示するか defer する）
  根拠は `regime-lens-replay-2026-05.md` の 4w mean rel −4pt と `reports/2026-06-17-trade-strategy-rootcause.md` の counterfactual。

## After writing

```bash
uv run baibai-loop-ledger sync --root .
uv run baibai-loop-validate --target ledger
uv run baibai-loop-validate
```
