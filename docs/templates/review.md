---
ticker: "XXXX"
decision_event_id: decision-YYYYMMDD-XXXX-review-target
research_ref: records/05-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
trade_ref: records/06-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
playbook_id: valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth
classification: success | failure | invalidated | inconclusive
verified_at: "YYYY-MM-DDTHH:MM:SS+09:00"
outcome:
  horizon_bd: 15
  start_price_basis: first_fill_vwap_yen | research_max_entry_price_yen | candidate_run_close_adjusted_close
  start_price_yen: 0
  end_price_yen: 0
  gross_return_pct: 0.0
  market_baseline_return_pct: 0.0
  sector_baseline_return_pct: 0.0
  market_relative_return_pct: 0.0
  sector_relative_return_pct: 0.0
  primary_relative_baseline: sector
  primary_relative_return_pct: 0.0
  execution_costs_yen: 0
  net_return_pct: 0.0
attribution_targets:
  - type: evidence_hit | macro_regime_gate | sizing | execution | playbook
    target_id: "id"
    effect: helped | hurt | neutral | unknown
    confidence: high | medium | low
structured_field_provenance:
  outcome: machine_calculated
  attribution_targets: analyst_written | llm_drafted_analyst_confirmed
---

# Review: YYYY-MM-DD XXXX [銘柄名]

**成分**: Decision lifecycle の **reviews / attribution**（[`/docs/components/reviews.md`](/docs/components/reviews.md)）

**Trade**: [records/06-trades/YYYY/MM/YYYY-MM-DD-XXXX.md](/records/06-trades/YYYY/MM/YYYY-MM-DD-XXXX.md)
**Research**: [records/05-research/YYYY/MM/YYYY-MM-DD-XXXX-*.md](/records/05-research/YYYY/MM/YYYY-MM-DD-XXXX-*.md)

## 1. Trade 概要

- Playbook: [valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth]
- Decision event: `decision_event_id`
- Review horizon: 15 / 30 business days
- Return basis: market / sector relative return
- Execution costs: commission / tax / slippage

## 2. 採用時 thesis の振り返り

- **原 thesis**（research から）: [1-2 段落転写]
- **実際に起きたこと**: [事実として記述]
- **Thesis の的中度**: [完全的中 / 部分的中 / 外れ]

## 3. Attribution classification

### 3.1 分類

- **Classification**: success / failure / invalidated / inconclusive
- **Primary attribution**: evidence_hit / macro_regime_gate / sizing / execution / playbook
- **Confidence**: high / medium / low

### 3.2 自由記述（必須）

[判断改善に使う要点。evidence / macro / sizing / execution / playbook のどれに帰属するかを明確にする]

## 4. +15 営業日レビュー（exit 日 + 15 営業日時点）

Review 実施日: YYYY-MM-DD

- exit 後の株価推移: [+X%, 上昇/下降トレンド継続/反転]
- 同業種の推移: [業種全体との相対パフォーマンス]
- マクロ環境の変化: [outlook に関連する変化があれば記録]
- 振り返り: [exit タイミングは適切だったか、早すぎた/遅すぎた]

## 5. +30 営業日レビュー（exit 日 + 30 営業日時点）

Review 実施日: YYYY-MM-DD

- exit 後の株価推移: [+X%]
- 1 か月後の整理: [thesis の妥当性、他銘柄との比較]
- Playbook へのフィードバック候補: [次周回で変更すべきポイント]

## 6. 月次 retro への引き渡し

本 review の要点を月次 retro ([`retro-YYYYMM.md`](/records/07-reviews/YYYY/retro-YYYYMM.md)) でまとめる:

- 成功/失敗分類と自由記述
- 四半期再分類の input 候補
- Playbook 改訂提案（あれば）

---

参照: [`/docs/components/reviews.md`](/docs/components/reviews.md), [`/docs/screening/failure-taxonomy.md`](/docs/screening/failure-taxonomy.md)
