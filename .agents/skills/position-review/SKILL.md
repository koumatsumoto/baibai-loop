---
name: position-review
description: 有限Operationの保有を再評価して提案を保持し、人間採用時に再検証・公開する。
---

# Position Review

保有一件の判断を行う。actionと残存見返りの意味は[Position Review reference](../../../docs/reference/position-review.md)に従う。新規・保有を合わせた固定選択の[Research進行](../../../docs/reference/analysis-operations.md#明示開始と固定対象)に従う。独立の上限で対象を追加しない。

## 手順

1. 人間の依頼範囲と対象holdingを確認する。売買報告の扱いは[Ledger Record](../ledger-record/SKILL.md#売買報告の扱い)に従い、既存台帳から対象を読む。全銘柄の価格更新や、無関係な入出金の再確認を前提にしない。
2. 混在Operationでは`research prepare`が作った`holdings/<ticker>`を使う。入力は子manifestの`domain_inputs_ref`が指す親`domain-inputs.yaml`を参照し、対象tickerの入力を使う。保有単独の明示開始では`research position-prepare --request '<USER_START_REQUEST>' --asof <ASOF> --ticker <TICKER>`でworkspaceと有限Operationを作り、自身の`domain-inputs.yaml`を読む。現行のReviewed Thesisがあれば`thesis-scaffold --from-thesis-id`で元資料と予測を引き継ぐ。旧版しかない場合は新しいdraftを作り、現在の証拠と独立Reviewを揃えて`--supersedes-id`で旧IDを参照する。自動変換で旧評価を実行しない。
3. 決算・guidance・資本政策・投資理由の変化を一次資料で確認し、[Researchの企業別調査](../research/SKILL.md#company-research)に従ってThesisとReviewをpromoteする。同じOperationの保有purposeとして継続し、新規CAA・購入Planningへ混ぜない。
4. `position position-review-build`で対象holdingと最新Reviewed Thesisを組み立て、`remaining_reward`と経済的な理由を記入する。現在からの増分、分配、期間の扱いはreferenceに従う。
5. `position position-review --input <draft>`でcheckする。対象の数量・basis・quote・Thesisが変わった場合は再確認し、提出したremainingを機械値で上書きしない。
6. `operation record-position-proposal <OPERATION_ID> --draft <DRAFT> --sqlite-path <MARKET_DB>`で現在入力を検証してdraftと参照・検証時点をOperationへ保持する。人間確認済みpublicationは作らない。調査未完了なら`operation record-unfinished`で理由・参照を残す。L3提案完成は[Researchの完了](../research/SKILL.md#5-operationを完了する)で記録する。
7. 人間の採用時に`position position-review publish <draft> --thesis-id <THESIS_ID> --confirmed`で公開する。未確定は理由付きnullとして保存し、暗黙のholdにしない。
8. 実売却・部分売却・追加購入が人間から報告されたら[Ledger Record](../ledger-record/SKILL.md)で記録する。公開actionを約定としない。必要な監視taskとcloud反映は[Ops Maintenance](../ops-maintenance/SKILL.md)に従う。

引数は各public `--help`を参照する。重要な入力矛盾や人間未確認のままpublishせず、数量basis未確認なら数量付き売却案を示さない。
