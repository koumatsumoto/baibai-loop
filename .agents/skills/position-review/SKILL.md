---
name: position-review
description: 有限Researchの保有scopeで評価を確認し、継続・売却・保留のInvestment Proposalを作る。
---

# Position Review

保有見直しは[Research](../research/SKILL.md)のreview_positions入口から開始する。actionと残存見返りの意味は[Investment Proposalの保有契約](../../../docs/reference/investment-proposal.md#holding)が正本である。対象を売却すると先に決めず、既存ledgerから現在保有を確認し、固定選定の範囲を独断で広げない。

1. 依頼範囲とholdingを確認し、Researchのselection-context→prepareで有限対象を固定する。holdings/<ticker>子workspaceを使い、親domain-inputsの保存snapshotを読む。全保有quoteの価格更新や無関係な入出金の再確認を企業調査の前提にしない。
2. 決算・guidance・資本政策・投資理由の変化と反証を一次資料で確認し、[共通の企業調査](../research/SKILL.md#company-research)でStock Assessmentと独立Reviewを公開する。適用可能な保存pairはoperation record-assessmentで正確に束縛できる。差分refreshでは旧source/fact日付を保持し、期間と今後分配を確認して新Reviewを取る。
3. proposal-contextとproposal-scaffoldで現在holding、quote、評価pairと固定scopeを読む。今回の仮説維持または反証確認理由を記し、hold/sell/deferを区別する。仮説維持のholdと当日の十分な収益算術を分け、不明を暗黙のholdへしない。brokenは価格不足でholdにせず、sell判断とterms未確定を分ける。
4. proposal-checkで入力・算術・条件を確認し、作者と別の役が全文draft hashへ内容Reviewを行う。修正時は再Reviewし、proposal-publishで保存する。AI提案の公開には人間の採用confirmを混ぜず、予約・注文・約定を作らない。
5. operation record-proposalで公開ID/hashを記録し、[Researchの完了](../research/SKILL.md#5-operationを完了する)へ進む。具体売却条件が足りない場合は未完了理由を明示し、価格・数量まで完成したと報告しない。
6. 本人が採否とbroker実状態を確認する。実売却・部分売却・追加購入の報告だけを[Ledger Record](../ledger-record/SKILL.md)へ渡す。保存提案と現在previewを分け、現在値で当時の条件を書き換えない。

必要な監視taskとcloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。重要な入力矛盾や独立Review未完了は解消するまで進めない。数量basis不明なら数量付き売却案を示さない。
