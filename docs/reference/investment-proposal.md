# Investment Proposal

保存済みの[企業評価](stock-assessment.md)を、確認した価格・cash・保有・注文へ適用した行動と条件。公開は人間の採用・予約・注文・約定を作らない。操作順は[Research skill](../../.agents/skills/research/SKILL.md)、modelと現在previewは`research/proposal.py`・`proposal_query.py`、保存は`proposal_service.py`が所有する。

## 行動と比較

buyは現在closeを上限とする指値または低い指値待ち、holdは今回確認した仮説維持/十分な残存見返り、sellは重要な仮説不成立/確認済みの見返り不足による全量回収。no_actionは完成した比較による経済的見送り、deferは調査・入力・判断条件の不足である。

固定Operationの全対象を完成評価または理由・参照付き未完了で被覆し、new/holdingを混ぜない。未調査は評価参照なしのdefer。独立Review済みuncertain/unresolvedの評価も参照付きdeferにできる。0対象に空提案を作らない。

価値獲得経路、Base/Downside、反証、実現時間を現金維持や他の完成案と比べる。同じBase年率でも倍率上昇と利益回復への依存を分け、Pmax内だけで採用しない。企業本文を再掲せずexact評価へ辿る理由にする。重要なtechnical所見は行動理由と見直し条件へつなぎ、価格局面だけでPmaxやsell条件を変えない。

ProposalはReviewed Assessment全文を参照する。未評価のSelection L2 blockを直接足す設計は採らず、重要な支持・相反・不足を評価と独立Reviewへ通す。[採用理由と有限検証](../../reports/studies/2026-10-22-l3-four-domain-integration/report.md)を参照する。

## 条件付き購入

実quoteは観測、提案limitは試算価格。安い架空quoteで不足を埋めない。Baseの終価・今後分配・期間と[案件ごとの要求率](../doctrine.md#required-return-policy)によるPmax以下で、仮説・権利basis・cash・既保有・予約・同提案の約定等の条件を確認する。算術のownerは`research/entry_policy.py`。

現在closeがPmax超過でも低いlimitで待てる。ただし購入の経済判断をまだ採らず、値下がり時に再検討するだけの監視価格はbuyにしない。価格以外の重要な不足を指値で迂回せず、完成した見送りはno_action、不足はdeferで理由と再開条件を残す。

数量はlimit・guide上限・確認済みcashの単元床から求める。guideを超える1単元もcash内ならwarning付き候補となり、下限を満たすだけの増量はしない。通常は既保有・active買予約への追加提案をせず、別評価IDで迂回しない。同提案の約定後は全売却済みでも再利用せず新しいResearchで判断する。

<a id="price-grid"></a>
### 具体条件

価格・数量・対象session・前提を保存する。共通gridはJPX「その他」表を使う保守的な案で、TOPIX500の最良呼値や注文受理・約定の保証ではない。分類・確認期間等が不足すれば経済判断とterms未確定を分ける。作者の希望limitを後日のquoteから推測せず、価格・数量まで未確定なら具体提案完成と報告しない。

実注文の任意期限は人間が確認する取引事実で、対象sessionから補完しない。現在close上限のlimitも即時約定を意味しない。

<a id="holding"></a>
## 保有継続と回収

重要な一次根拠で投資理由の重大なbreakを確認したらsellを優先する。quote・valuation・数量basisが不足してもholdへ変えず、売却termsの不足を残す。当初理由は確認できるexact参照を使い、現在評価で置き換えない。

今回intactを確認したholdは、旧valuation・quote欠測・算術未確定でも可能だが、当日の収益が十分という主張ではない。十分な残存見返りのhold／不足によるsellには当日起点valuationと現在算術が必要。仮説維持holdでも非nullの見返り主張は同日算術で確認する。判断が不確実ならdeferとする。

残存見返りは今後保有して得る増分。受取済みcashと、今売っても受け取れる確定済み未入金配当を再算入せず、旧horizonの割り直しで年率を水増ししない。税・費用・遅延・下振れが結論を変えるなら不確実性を示す。

価格下落、期間経過、回復遅延、Target到達、新規購入条件未達、他候補・集中warning単独では売らない。現金回収に次候補は不要。初期のsellは全量で、端株や数量不明を黙って丸めない。active予約があれば経済sellと注文処理確認を分け、人間報告を台帳へ反映して具体条件を再確認する。

<a id="publication-review"></a>
## Review・保存・preview

scaffoldで入力とIDを固定し、正規checkが返すdraft_sha256へ作者と別の内容Reviewを束縛する。checkのvalidとreview_bindingは別で、staleやfile hashをReview完了としない。価格・数量・cash・scope・warningを含む内容を変えたら再Reviewする。初回発行直前のpublished_atを使い、Review時刻を付け替えて順序を作らない。

公開時は固定scope・exact評価pair・現在の入力事実を照合する。checked_atの経過だけをdriftとせず、実際の入力変化でsnapshotとReviewを取り直す。同ID同内容の再送は保存済み結果を返し、異内容は拒否する。読取へ現在termsの適格性を追加して過去本文を書き換えない。

previewは保存条件を保ち、現在quote・cash・limit再検証・warning・不足理由を別に示す。利用不能でも原評価・当時の条件付き算術を失効・誤りと断定しない。

集中・ADV・budget・cash warningは人間へ示す。保有quoteや権利basisが揃わなければNAVを作らず、既知factor比率が下限の場合はcoverageを示す。saved warningとcurrent warningを混ぜない。実取引は[Ledger](portfolio-ledger.md)へ別途記録する。
