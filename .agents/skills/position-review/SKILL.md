---
name: position-review
description: 決算、material event、経済的投資理由の不成立、残存見返りの変化を受けて保有を再評価し、hold / exit / nullを発行する。実約定の記録はledger-record skill。
---

# Position Review

対象tickerの企業評価と現在からの残存見返りを確認する。型・actionの正本は[Position Review reference](../../../docs/reference/position-review.md)。価格下落、経過期間、新規買いfloor未達、Target到達、集中warningや他候補単独で売らない。

## 手順

1. `git status --short --branch`と`position ledger`の価格不要のcash・数量・原価を確認し、対象holdingと人間の依頼範囲を確定する。時価未評価は事実記録の不正ではない。新しいOperationを開始せず、activeな資本調査があっても対象保有を扱う。全holding価格の事前applyは不要。
2. `research position-prepare`で対象holdingのworkspaceを作る。利用可能な最新v4のReviewed Thesisがあれば`thesis-scaffold --from-thesis-id`で元資料の日付・予測を保持した差分を作る。旧版しかない場合は`--from-thesis-id`なしで新規v4 draftを作り、現在の証拠で評価し直す。旧資料は参考にできるが、自動変換せず、promote時に旧IDを`--supersedes-id`で指定する。正式評価日は当日、quoteは前営業日でよい。
3. 直近Thesisから変わった決算・guidance・資本政策と重大な不成立条件を一次資料で確認する。据置guidanceを十分な進捗と決めつけず、当該四半期と過去同四半期の通期進捗を比較する。source・権利単位・Base/Downsideと独立Reviewは[research skill](../research/SKILL.md)に従い、一組でpromoteする。旧Thesisを現行modelへ変換して実行しない。
4. `position position-review-build`で対象holdingと最新Reviewed Thesisを組み立てる。作者が`remaining_reward`のsufficient/insufficient/uncertainと経済的理由を記入する。受取済み・権利確定済み未入金の分配を将来増分へ二重計上せず、旧horizon短縮で年率を水増ししない。
5. `position position-review --input <draft>`でcheckする。提出したremainingを機械値で上書きしない。対象数量・cost・重要なbasis・quote・最新Thesisの変更は再確認する。無関係なledger更新だけで企業調査をやり直さない。
6. 人間の確認後だけ`position position-review publish <draft> --thesis-id <THESIS_ID> --confirmed`で公開する。理由付きnullも保存できる。重要なbreakを確認した場合はquote/remaining不明でもexit候補とし、数量basis不明なら数量付き売却案は示さない。
7. 必要な監視だけをdated taskにし、実売却・部分売却・追加購入の報告後はledger-recordで事実を記録する。公開actionを約定とみなさない。cloud反映はops-maintenanceに従う。

引数は各public `--help`で確認する。source不整合・人間未確認ではpublishしない。企業価値や価格を評価できないことは理由付きnullの正常な結論であり、暗黙のholdに変換しない。

## 参照

- [Thesis](../../../docs/reference/thesis.md)
- [資本規律](../../../docs/portfolio-management.md)
- [Position Review](../../../docs/reference/position-review.md)
