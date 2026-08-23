# method/research/playbooks/

Opportunity LaneまたはEvidence Patternに適用する人間向けresearch checklist。
active Markdownは`research_playbook_id`と、明示的な
`applies_to_opportunity_lane_ids` / `applies_to_evidence_pattern_ids`を持ち、少なくとも一方を
non-emptyにする。同名slugによるimplicit mappingは行わない。mapping変更は新しいtimestamp版を作る。
機械的な閾値・除外条件・選定順はscreening rulesが正本で、この領域はH2見出しの強制や
runtime loaderを持たない。

## Layout

`method/research/playbooks/<directory-slug>/YYYY-MM-DDTHHMMSS+0900.md`

## Active Patterns

| `research_playbook_id` | Focus | Checklist |
| --- | --- | --- |
| `cash-rich-asset-discount-research-v1` | 現金・純資産に対する割安 | [`2026-08-24T000000+0900.md`](./cash-rich-asset-discount/2026-08-24T000000+0900.md) |
| `cashflow-yield-discount-research-v1` | 営業キャッシュフロー利回りの割安 | [`2026-08-24T000000+0900.md`](./cashflow-yield-discount/2026-08-24T000000+0900.md) |
| `sales-discount-growth-research-v1` | 売上成長を伴う P/S 割安 | [`2026-08-24T000000+0900.md`](./sales-discount-growth/2026-08-24T000000+0900.md) |
| `valuation-reversion-research-v1` | 業種・自己レンジ対比の valuation 割安 | [`2026-08-24T000000+0900.md`](./valuation-reversion/2026-08-24T000000+0900.md) |
