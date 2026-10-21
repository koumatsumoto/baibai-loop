# Research playbooks

Valuation ApproachからFundamental Researchへ渡す、人間向けresearch checklistを所有します。機械的なeligibility・approach内順位・候補集合の構成はscreening rulesの責務です。

| 項目 | 内容 |
| --- | --- |
| 所有 | 問うべきclaim、確認するsource |
| 所有しない | screening閾値、候補選定順、runtime loader、Stock Assessmentの企業評価、Investment Proposalの行動判断 |
| 入口 | `method/research/playbooks/<directory-slug>/YYYY-MM-DDTHHMMSS+0900.md` |
| 依存境界 | 各revisionは`research_playbook_id`とnon-emptyな`applies_to_valuation_approach_ids`を持つ。slugによるimplicit mappingは禁止 |
| 変更先 | mappingを含む変更は既存dated methodを編集せず、新しいtimestamp版を作る |
| 正本・test | [research skill](../../../.agents/skills/research/SKILL.md)、[Stock Assessment](../../../docs/reference/stock-assessment.md)、[playbook contract test](../../../tests/contracts/test_research_playbook_contract.py) |

<a id="work-state"></a>

## 調査と評価の区別

playbookはsourceとclaimを確認するための補助であり、独立した進捗台帳ではありません。期待した証拠が得られなくても、不足と評価への影響を説明できる場合はStock Assessmentの`uncertain`として評価できます。評価自体を完了できない場合はOperationへ`unfinished`と理由を記録します。`supported / uncertain / unsupported`という企業評価と、Investment Proposalの`defer`などの行動を混同しません。

未確認情報をverifiedとして扱わず、公開はStock Assessmentと独立Assessment Reviewの内容で検証します。別checklistの全行完了を公開条件にしません。進行・再開・完了の操作は[Research skill](../../../.agents/skills/research/SKILL.md)、意味は[Analysis operations](../../../docs/reference/analysis-operations.md)を参照してください。

## Active revisions

| `research_playbook_id` | 対象 | revision |
| --- | --- | --- |
| `current-earnings-power-research-v1` | current earnings power | [`2026-08-30T000000+0900.md`](./current-earnings-power/2026-08-30T000000+0900.md) |
| `normalized-earnings-power-research-v1` | normalized earnings power | [`2026-08-30T000000+0900.md`](./normalized-earnings-power/2026-08-30T000000+0900.md) |
| `asset-value-research-v1` | asset backing | [`2026-08-30T000000+0900.md`](./asset-value/2026-08-30T000000+0900.md) |
| `reinvestment-value-research-v2` | reinvestment value | [`2026-10-13T232400+0900.md`](./reinvestment-value/2026-10-13T232400+0900.md) |
