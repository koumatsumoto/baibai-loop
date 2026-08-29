# Research playbooks

Evidence Patternに適用する、人間向けresearch checklistを所有します。機械的な閾値・除外・選定順はscreening rulesの責務です。

| 項目 | 内容 |
| --- | --- |
| 所有 | 問うべきclaim、確認するsource、調査進捗 |
| 所有しない | screening閾値、候補選定順、runtime loader、Thesisの最終判断 |
| 入口 | `method/research/playbooks/<directory-slug>/YYYY-MM-DDTHHMMSS+0900.md` |
| 依存境界 | 各revisionは`research_playbook_id`とnon-emptyな`applies_to_evidence_pattern_ids`を持つ。slugによるimplicit mappingは禁止 |
| 変更先 | mappingを含む変更は既存dated methodを編集せず、新しいtimestamp版を作る |
| 正本・test | [research skill](../../../.agents/skills/research/SKILL.md)、[Thesis](../../../docs/reference/thesis.md)、[playbook contract test](../../../tests/contracts/test_research_playbook_contract.py) |

<a id="work-state"></a>

## 調査状態

- `pending`: 未着手
- `blocked`: 判断に必要な調査が未完了
- `complete`: 調査が完了

期待した証拠が得られなくても、未確認であることとdispositionへの影響を`note`またはThesisへ残せば`complete`です。未確認情報をverifiedとして扱ってはいけません。`promote`は全項目が`complete`の場合だけ許可します。

## Active revisions

| `research_playbook_id` | 対象 | revision |
| --- | --- | --- |
| `cash-rich-asset-discount-research-v1` | 現金・純資産に対する割安 | [`2026-08-24T000000+0900.md`](./cash-rich-asset-discount/2026-08-24T000000+0900.md) |
| `cashflow-yield-discount-research-v1` | 営業キャッシュフロー利回りの割安 | [`2026-08-24T000000+0900.md`](./cashflow-yield-discount/2026-08-24T000000+0900.md) |
| `sales-discount-growth-research-v1` | 売上成長を伴うP/S割安 | [`2026-08-24T000000+0900.md`](./sales-discount-growth/2026-08-24T000000+0900.md) |
| `valuation-reversion-research-v1` | 業種・自己レンジ対比のvaluation割安 | [`2026-08-24T000000+0900.md`](./valuation-reversion/2026-08-24T000000+0900.md) |
