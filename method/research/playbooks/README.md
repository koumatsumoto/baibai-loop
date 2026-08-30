# Research playbooks

Valuation ApproachからFundamental Researchへ渡す、人間向けresearch checklistを所有します。機械的なeligibility・approach内順位・Review Set構成はscreening rulesの責務です。

| 項目 | 内容 |
| --- | --- |
| 所有 | 問うべきclaim、確認するsource、調査進捗 |
| 所有しない | screening閾値、候補選定順、runtime loader、Thesisの最終判断 |
| 入口 | `method/research/playbooks/<directory-slug>/YYYY-MM-DDTHHMMSS+0900.md` |
| 依存境界 | 各revisionは`research_playbook_id`とnon-emptyな`applies_to_valuation_approach_ids`を持つ。slugによるimplicit mappingは禁止 |
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
| `current-earnings-power-research-v1` | current earnings power | [`2026-08-30T000000+0900.md`](./current-earnings-power/2026-08-30T000000+0900.md) |
| `normalized-earnings-power-research-v1` | normalized earnings power | [`2026-08-30T000000+0900.md`](./normalized-earnings-power/2026-08-30T000000+0900.md) |
| `asset-value-research-v1` | asset backing | [`2026-08-30T000000+0900.md`](./asset-value/2026-08-30T000000+0900.md) |
| `reinvestment-value-research-v1` | reinvestment value | [`2026-08-30T000000+0900.md`](./reinvestment-value/2026-08-30T000000+0900.md) |
