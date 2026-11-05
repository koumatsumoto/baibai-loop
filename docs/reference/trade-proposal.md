# Trade Proposal

保存済みの[銘柄評価](stock-assessment.md)を、確認した価格・cash・保有・注文へ適用した行動と条件。公開は人間の採用・予約・注文・約定を作らない。操作順は[Investment Judgment skill](../../.agents/skills/investment-judgment/SKILL.md)、契約・算術・保存分岐は[提案owner](../../engine/src/baibai_engine/investment_judgment/trade_proposal/)が所有する。

## 行動と比較

buyは作者が明示した上限での指値提案、holdは今回確認した仮説維持/十分な残存見返り、sellは重要な仮説不成立/確認済みの見返り不足による全量回収。no_actionは完成した比較による経済的見送り、deferは調査・入力・判断条件の不足である。

作成するProposalは固定Operationの全対象を被覆し、new/holdingを混ぜない。未調査は理由・参照付き未完了に対応する評価参照なしのdefer。独立検証済みuncertain/unresolvedの評価も参照付きdeferにできる。0対象に空提案を作らない。

価値獲得経路、Base/Downside、反証、実現時間を投下額に照らし、確認済み保有との共通要因・同時損失を含む[全体の経済性](../doctrine.md#economic-return-policy)として現金維持や他の完成案と比べる。同じBase年率でも倍率上昇と利益回復への依存を分け、Pmax内だけで採用しない。企業本文を再掲せずexact評価へ辿る理由にする。重要なテクニカル分析の所見は行動理由と見直し条件へつなぎ、価格局面だけでPmaxやsell条件を変えない。

ProposalはReviewed Assessment全文を参照する。未評価のSelection L2 blockを直接足すとReviewを経ない判断根拠が入り得るため、重要な支持・相反・不足を評価と独立検証へ通す。

## 条件付き購入

観測quote、価値と損失経路から引き受ける許容価格域、今回のlimit、実約定価格を分ける。quoteは未調整終値で、最良売気配ではない。許容価格域は既存評価を参照して説明し、別の必須fieldや自動gateにしない。安い架空quoteで不足を埋めず、評価・権利basis・資本条件とlimitの算術を確認する。

`place_limit`は作者指定の買い指値を今提示する区分で、観測quoteより高い上限も指定できる。上昇を待つstop条件でも、注文・予約・約定の記録でもなく、価格への自動上乗せはしない。[買い指値は上限以下で買う条件](https://www.jpx.co.jp/glossary/sa/174.html)であり、必ず上限を支払うとも、quote近辺で買えるとも限らない。具体termsの`at_current_limit`は現在closeの呼値切下げと一致する指値、`wait_limit`は観測quoteより低い指値である。

PmaxはBaseの終価・今後分配・期間と[案件ごとの要求率](../doctrine.md#required-return-policy)から求める価格上限で、期待値・勝率・最適指値・期待約定価格ではない。上振れを含めた経済判断が魅力的でも、そのlimitでのBase年率が要求率未達なら現行gateは通さない。経済判断とgateが食い違う場合は、実装上の制約として区別して残し、Base・要求率・期間の通過用変更で迂回しない。

現在closeがPmax超過でも低いlimitで待てる。ただし購入の経済判断をまだ採らず、値下がり時に再検討するだけの監視価格はbuyにしない。評価不能や具体条件の不足を指値で迂回せず、完成した見送りはno_action、未完了の判断はdeferで理由と再開条件を残す。重要な不確実性があっても、[引受理由](stock-assessment.md#uncertainty-and-decision)を持つsupported/resolvedの評価からbuyを提案できる。提案理由には見込む条件付き利回り、主要な損失経路、なぜ今そのリスクに見返りが見合うかを示し、未確定事項の列挙だけで終えない。

作者は[資本方針](../doctrine.md#required-return-policy)に従い、limit・guide・確認済みcashによる数量上限の内側で単元数を選ぶ。既保有・active買予約への追加や、別評価IDでの迂回はしない。同提案の約定後は全売却済みでも再利用しない。

<a id="limit-selection"></a>
### 指値と待機案の選択

許容価格域の中で、見返り・単元数/cash・既知イベントによって判断が変わる少数のlimit候補を、現金維持や他の完成案と比較する。高い上限、低い指値待ち、見送りのいずれも選べ、全呼値の探索や固定数の候補を要求しない。採用理由と、採らない案の重要な差・選択が反転する条件を既存rationale・comparison・forgoneへ記し、同文を各欄へ複写しない。

候補比較は[同じ提案context](../../engine/src/baibai_engine/investment_judgment/trade_proposal/inputs.py)と[正準の購入算術](../../engine/src/baibai_engine/investment_judgment/trade_proposal/conditions.py)を使う。鮮度はquote、支出上限と提案利回りは作者limitで確認し、quote/Pmaxの計算を任意limitの計算としない。価格・保有株数・評価価格のbasisは一つの市場snapshotへ揃え、入力が変われば比較を取り直す。

小口・上限量・現金維持の重要差を比較し、小口を安全な既定にはしない。通常のno-addでは後で保有を増やせないため、最初に買わない分の上振れを放棄する代償を既存comparison・forgoneへ含める。同価格での一株returnは同じでも、損益金額・残cash・集中・ADVは選択数量で変わる。

未約定で失い得る利益だけでなく、保持cash・他の投資機会・回避できる損失と、待って得られる情報を比べる。悪材料の時だけ指値が成立する逆選択やevent gapも考え、約定率×Base利益だけで選ばない。同じ毀損を価値と別penaltyで二重控除せず、機会損失を未約定後の最高値で事後定義しない。ADVや日足から板・queue・価格impact・精密な約定確率を創作せず、`low <= limit`も約定保証にしない。精密確率や全リスク解消を要件にせず、推定を使う場合は[根拠・幅・感応度](../doctrine.md#fact-analysis-separation)を示す。

対象session・limitの前提と既知イベント、未約定・部分約定・新情報の後に何をいつ確認するかを記す。事実・仮説が変われば評価やsnapshotへ戻り、新しい内容Reviewへ束縛する。部分約定後は確認済み保有・残注文・cashと既保有追加禁止を含む現行policyで判断し、残数を自動で再提案しない。未約定を理由に上限超へ追随したり、自動で引上げ・延長したりしない。

<a id="price-grid"></a>
### 具体条件

価格・数量・対象session・前提を保存する。共通gridはJPX「その他」表を使う保守的な案で、TOPIX500の最良呼値や注文受理・約定の保証ではない。分類・確認期間等が不足すれば経済判断とterms未確定を分ける。作者の希望limitを後日のquoteから推測せず、価格・数量まで未確定なら具体提案完成と報告しない。

対象sessionや提案の見直し時点はbrokerの実注文期限ではない。実注文の任意期限は人間が確認する取引事実で、提案から補完しない。[価格・時間優先と板寄せ/ザラバ](https://www.jpx.co.jp/equities/trading/domestic/04.html)等で結果が変わり、上限の提示は約定・即時約定を保証しない。

<a id="holding"></a>
## 保有継続と回収

重要な一次根拠で投資理由の重大なbreakを確認したらsellを優先する。quote・valuation・数量basisが不足してもholdへ変えず、売却termsの不足を残す。当初理由は確認できるexact参照を使い、現在評価で置き換えない。

今回intactを確認したholdは、旧valuation・quote欠測・算術未確定でも可能だが、当日の収益が十分という主張ではない。十分な残存見返りのhold／不足によるsellには当日起点valuationと現在算術が必要。仮説維持holdでも非nullの見返り主張は同日算術で確認する。仮説を評価しきれない場合はdeferとし、将来に幅があることだけでholdを取り消さない。

残存見返りは今後保有して得る増分。受取済みcashと、今売っても受け取れる確定済み未入金配当を再算入せず、旧horizonの割り直しで年率を水増ししない。税・費用・遅延・下振れが結論を変えるなら不確実性を示す。

sellのlimitは作者が受け入れる一株当たりの回収下限。実quoteを保持したまま、その上下どちらも指定できるが、共通gridへ正確に適合させ、下限を黙って切り下げない。notionalは全量が条件内で約定した場合の税・費用前回収額の下限であり、予想約定額・確定cashではない。下限での回収、高い価格を待つ代償、保有継続を既存rationale・comparison・forgoneで比較し、未約定時の再確認をreview_conditionsへ記す。

`remaining_reward`は現在の実quoteでの経済判断を維持する。不足によるsellでは、同じ評価・当日起点の期間と今後分配で、作者下限を受け取らず保有を続ける条件付き見返りも比較する。checkとpreviewの`sell_limit_projection`はこの機会費用上の算術で、下限を観測quoteや売却後の収益としない。下限を下げると保有の魅力が戻る場合は、下限引上げ・hold・再確認と比べ、なお回収する理由を内容Reviewへ通す。Base要求率から自動Pminを作らず、Pmaxをsell gateにしない。現在は十分なのに高い指値到達時だけ不足となるTarget売りは本policyに含めない。brokenのsellには無効になったvaluationでの下限算術を必須にしない。

価格下落、期間経過、回復遅延、Target到達、新規購入条件未達、他候補・集中warning単独では売らない。現金回収に次候補は不要。初期のsellは全量で、端株や数量不明を黙って丸めない。active予約があれば経済sellと注文処理確認を分け、人間報告を台帳へ反映して具体条件を再確認する。

<a id="publication-review"></a>
## Review・保存・preview

作者と別の内容Reviewは正規checkのdraft_sha256へ束縛し、機械検査のvalidやfile hashと区別する。価格・数量・cash・scope・warningを含む内容を変えたら再Reviewする。公開時刻はエンジンが初回発行時に付ける。Reviewも実際の検証時刻を記し、時刻の付替えで順序を作らない。

公開時は固定scope・exact評価pair・現在の入力事実を照合する。再読時刻だけの差を入力変化とせず、事実が変わればsnapshotとReviewを取り直す。現在条件を過去本文・hashへ適用せず、旧保存を新しい下限比較済み判断へ読み替えない。未公開draftの入力差は再snapshot・再Reviewする。

previewは保存条件と現在の再試算を分ける。買付の保存limit・数量を現在上限で再検証し、超過しても黙って縮小しない。現在quoteでの別試算を保存案の変更とせず、利用不能でも当時の評価・算術を誤りと断定しない。

集中・ADV・budget・cash warningは人間へ示す。保有quoteや権利basisが揃わなければNAVを作らず、既知factor比率が下限の場合はcoverageを示す。saved warningとcurrent warningを混ぜない。実取引は[Ledger](portfolio-ledger.md)へ別途記録する。

## 提案の訂正と人間の報告

人間の非購入はシステムのno_actionではなく、exact提案への別記録である。本人の発言・報告日時・明示された対象時点と担当者の要約を区別し、不明を補完しない。項目は[記録契約](../../engine/src/baibai_engine/investment_judgment/trade_proposal/record_contracts.py)、操作はskillを参照する。未発注・取消・未約定・永久却下を推定せず、提案・Review・台帳を変更しない。

誤記録はexact参照と理由を持つ追記訂正で最新候補から除外する。関連する以前の提案へ優先権を与えず、残りを元の基準日・発行時刻・ID順で選ぶ。訂正時刻による再ランキング、新しい買付推奨、現在条件の適格性回復は行わない。両原本はIDで読め、完了Operationも書き換えない。

保存表示と現在previewは同じ訂正集合を使い、人間報告を別に示す。原提案が古い場合の当日起点refresh等は引き続き必要である。
