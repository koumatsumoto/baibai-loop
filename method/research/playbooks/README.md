# method/research/playbooks/

Screening evidence pattern ごとの人間向け research checklist。各 Markdown の
Research Playbookの`playbook_id`は、`method/screening/rules/*.yaml`とcandidatesの
`evidence_hits[].evidence_pattern_id`にある同名のMachine Evidence Patternへ対応する。機械的な閾値・除外条件・選定順は
screening rules が正本で、この領域は H2 見出しの強制や runtime loader を持たない。

## Layout

`method/research/playbooks/<playbook_id>/YYYY-MM-DDTHHMMSS+0900.md`

## Active Patterns

| `playbook_id` | Focus | Checklist |
| --- | --- | --- |
| `cash-rich-asset-discount` | 現金・純資産に対する割安 | [`2026-05-01T000000+0900.md`](./cash-rich-asset-discount/2026-05-01T000000+0900.md) |
| `cashflow-yield-discount` | 営業キャッシュフロー利回りの割安 | [`2026-05-01T000000+0900.md`](./cashflow-yield-discount/2026-05-01T000000+0900.md) |
| `sales-discount-growth` | 売上成長を伴う P/S 割安 | [`2026-05-01T000000+0900.md`](./sales-discount-growth/2026-05-01T000000+0900.md) |
| `valuation-reversion` | 業種・自己レンジ対比の valuation 割安 | [`2026-05-01T000000+0900.md`](./valuation-reversion/2026-05-01T000000+0900.md) |
