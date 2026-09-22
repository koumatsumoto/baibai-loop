---
name: position-review
description: 対象holdingを再評価してhold・exit・未確定を発行する。実約定はledger-recordで記録する。
---

# Position Review

保有一件の判断を行う。actionと残存見返りの意味は[Position Review reference](../../../docs/reference/position-review.md)に従う。新しいOperationは作らず、activeな資本調査とは別に対象holdingを扱う。

## 手順

1. 人間の依頼範囲と対象holdingを確認する。売買報告の扱いは[Ledger Record](../ledger-record/SKILL.md#売買報告の扱い)に従い、既存台帳から対象を読む。全銘柄の価格更新や、無関係な入出金の再確認を前提にしない。
2. `research position-prepare`でworkspaceを作る。現行のReviewed Thesisがあれば`thesis-scaffold --from-thesis-id`で元資料と予測を引き継ぐ。旧版しかない場合は新しいdraftを作り、現在の証拠と独立Reviewを揃えて`--supersedes-id`で旧IDを参照する。自動変換で旧評価を実行しない。
3. 決算・guidance・資本政策・投資理由の変化を一次資料で確認し、[Researchの企業別調査](../research/SKILL.md#company-research)に従ってThesisとReviewをpromoteする。ここから新規Research Set・CAA・購入Planningへ進まない。
4. `position position-review-build`で対象holdingと最新Reviewed Thesisを組み立て、`remaining_reward`と経済的な理由を記入する。現在からの増分、分配、期間の扱いはreferenceに従う。
5. `position position-review --input <draft>`でcheckする。対象の数量・basis・quote・Thesisが変わった場合は再確認し、提出したremainingを機械値で上書きしない。
6. 人間の確認後に`position position-review publish <draft> --thesis-id <THESIS_ID> --confirmed`で公開する。未確定は理由付きnullとして保存し、暗黙のholdにしない。
7. 実売却・部分売却・追加購入が人間から報告されたら[Ledger Record](../ledger-record/SKILL.md)で記録する。公開actionを約定としない。必要な監視taskとcloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。

引数は各public `--help`を参照する。重要な入力矛盾や人間未確認のままpublishせず、数量basis未確認なら数量付き売却案を示さない。
