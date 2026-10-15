---
name: position-review
description: Researchの保有scopeを開始し、保有固有の仮説・残存見返りを確認する。
---

# Position Review

[Research](../research/SKILL.md)の`review_positions`入口を使い、選定から独立Review・Investment Proposal・Operation完了まで共通手順を実行する。対象を売却すると先に決めず、現在ledgerから保有を確認する。固定scopeを独断で広げず、新規買付へ混ぜない。

保有固有の判断は[Investment Proposalの保有契約](../../../docs/reference/investment-proposal.md#holding)に従う。今回確認した仮説維持と当日の十分な残存見返りを分け、hold / sell / deferを記す。重大な反証を確認した場合は、価格不足を理由にholdへ変えず、sell判断と具体条件の不足を分ける。数量basis不明なら数量付き売却案を作らない。

保有workspaceは`holdings/<ticker>`で、親の固定入力を共有する。全保有quote更新や無関係な入出金再確認を企業調査の前提にしない。人間が報告した実取引は[Ledger Record](../ledger-record/SKILL.md)へ渡す。
