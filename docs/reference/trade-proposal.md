# Trade Proposal

[exact評価pair](stock-assessment.md#independent-second-pass)を確認済み価格・cash・保有/注文へ適用する。未評価のSelection L2を直接根拠へ足さず、重要材料は評価/Reviewへ戻す。公開は採用・予約・注文・約定を作らない。操作は[skill](../../.agents/skills/investment-judgment/SKILL.md)、契約/算術は[提案owner](../../engine/src/baibai_engine/investment_judgment/trade_proposal/)を参照する。

## 行動と比較

| 行動 | 意味 |
| --- | --- |
| buy | 作者の上限で買う指値提案。単なる監視価格ではない。 |
| hold | 今回確認した仮説維持、または十分な残存見返り。 |
| sell | 重大な仮説不成立、または確認済み見返り不足による全量回収。 |
| no_action | 完成した経済比較による見送り。 |
| defer | 調査・入力・判断条件の不足。 |

固定Operation全対象を被覆しnew/holdingを混ぜない。未調査は理由/参照付きunfinishedに対応する評価参照なしdefer、review済みuncertain/unresolvedは参照付きdeferにできる。0対象に空提案を作らない。

[全体の経済性](../doctrine.md#economic-return-policy)から、価値獲得経路・反証・期間・損益金額・既保有との共通損失を現金維持や他の完成案と比べる。同じBase年率でも倍率上昇/利益回復への依存は異なり、Pmax内だけで採用しない。本文を再掲せずexact評価へ辿る理由と見直し条件を残す。

## 条件付き購入

観測quote（未調整終値、売気配ではない）・根拠付き許容価格域・作者limit・実約定価格を分ける。架空の安いquoteで不足を埋めず、評価/権利basisとcashを確認する。許容価格域は説明であり新たな必須field/gateではない。

`place_limit`は今提示する買い指値で、quoteより高い上限も可能。上昇を待つstopでも自動上乗せでもない。[上限以下で買う条件](https://www.jpx.co.jp/glossary/sa/174.html)で、上限支払やquote近辺の約定を保証しない。`at_current_limit`はcloseの呼値切下げ一致、`wait_limit`はquoteより低い指値である。

PmaxはBase・期間・[要求率](../doctrine.md#required-return-policy)からの上限で、期待値/勝率/最適指値ではない。魅力的な上振れがあってもlimitのBase年率が要求率未達なら現行gateを通さず、その制約をBase/率/期間の通過用変更で迂回しない。quoteがPmax超でも低いlimitで待てるが、購入判断未了の監視はbuyにしない。

[受容理由](stock-assessment.md#uncertainty-and-decision)を持つsupported/resolvedから、見返り・主要損失経路・今引き受ける理由を示す。企業仮説の棄却とquote/cash等の不足を分け、未完はdefer、完成した見送りはno_actionとする。既保有・active買予約への追加、別評価IDによる迂回、約定済み提案の全売却後再利用はしない。

<a id="limit-selection"></a>
### 指値と待機案の選択

見返り・cash/単元・既知イベントで判断が変わる少数案を、現金維持/他案と比較する。全呼値探索や候補数ノルマは不要。採用理由・重要差・反転条件をrationale/comparison/forgoneへ重複なく残す。

比較は[同context](../../engine/src/baibai_engine/investment_judgment/trade_proposal/inputs.py)と[購入算術](../../engine/src/baibai_engine/investment_judgment/trade_proposal/conditions.py)を使う。鮮度は実quote、支出/利回りは作者limitで計算し、価格/株数basisを同一snapshotへ揃える。入力変更時は比較を取り直す。

数量は[資本方針](../doctrine.md#required-return-policy)に従う。小口・上限量・現金維持を比べ、同じ一株returnでも損益金額・残cash・集中・ADVとno-addによる放棄利益が異なることを示す。

待機は逃す利益だけでなく、保持cash・他機会・回避損失・追加情報を比較する。悪材料時だけ約定する逆選択/event gapも考え、約定率×Base利益だけで選ばない。価値に織込んだ毀損をpenaltyへ重ねず、機会損失を事後最高値で定義しない。ADV/日足から板・queue・impact・精密約定率を創作せず、`low <= limit`を約定保証にしない。確率推定は[根拠・幅・感応度](../doctrine.md#fact-analysis-separation)を示す。

session・前提/イベントと未約定・部分約定・新情報後の確認時点を残す。変化は評価/snapshotへ戻して再Review。部分約定後は実保有・残注文・cashとno-addで判断し、残数再提案・指値引上げ/延長・上限超追随を自動化しない。

<a id="price-grid"></a>
### 具体条件

価格・数量・session・前提を保存する。共通gridはJPX「その他」の保守案で、TOPIX500最良呼値や注文受理を保証しない。分類/確認期間等が不足すれば経済判断とterms不足を分け、limitを後日quoteから推測しない。価格/数量未確定を具体提案完成と呼ばない。

session/見直し時点はbroker期限ではなく、実期限は人間確認の取引事実。[価格・時間優先や板寄せ/ザラバ](https://www.jpx.co.jp/equities/trading/domestic/04.html)により結果が変わり、指値は即時約定を保証しない。

<a id="holding"></a>
## 保有継続と回収

当初理由は確認できるexact参照で読む。重要な一次根拠で重大なbreakを確認したらsellを優先し、quote/valuation/数量basis不足をholdへ変換しない。経済sellとterms不足は別である。

今回intactを確認したholdは旧valuationやquote欠測でも可能だが、当日の十分な見返りの主張ではない。十分/不足を述べるには同日起点valuationと現在算術が必要。仮説未評価はdeferとし、将来に幅があるだけでholdを取消さない。

残存見返りは今後保有する増分。受取済みcash、今売っても得る確定済み未入金配当を再算入せず、旧horizonの割直しで水増ししない。税・費用・遅延・下振れが結論を変えるなら扱う。

sell limitは作者が受け入れる一株回収下限。実quoteの上下とも可能だがgridへ正確に合わせ、黙って切り下げない。全量notionalは税費用前の条件付き回収下限で、予想約定額/確定cashではない。下限回収・高値待ち・保有継続と未約定後の再確認を記す。

`remaining_reward` sellは現在の実quoteでの不足判断に加え、同評価・期間・今後分配を作者下限で手放さず保有する見返りも比較する。`sell_limit_projection`はこの機会費用で、観測quoteや売却後収益ではない。下限で保有が魅力的になるなら下限引上げ/hold/再確認と比較し、なお回収する理由をReviewする。自動Pmin・Pmax sell gate・現在十分なのにTarget到達時だけ売る条件は作らない。broken sellへ無効valuationの下限算術を要求しない。

価格下落・期間経過・回復遅延・Target到達・新規購入条件未達・他候補/集中warning単独では売らず、回収に次候補も不要。sellは全量で端株/不明数量を丸めない。active予約は注文処理を別確認し、人間報告を台帳へ反映してtermsを再確認する。

<a id="publication-review"></a>
## Review・保存・preview

別作業者の内容Reviewを正規checkの`draft_sha256`へ束縛する。valid/file hashは内容Reviewではない。価格・数量・cash・scope・warning等の内容を変えたら再Reviewする。入力事実が変わる場合はsnapshotも取り直し、読取時刻だけの差は事実変更としない。検証は実時刻、公開時計はエンジンの初回発行値を使い、付替えて順序を作らない。

公開時に固定scope・exact評価pair・現入力を照合する。過去本文/hashへ現在条件を適用せず、旧保存を新しい下限比較済みにしない。previewは保存案と現在再試算を別表示し、保存limit/数量が現在上限超でも縮小せず、利用不能を当時の誤りと即断しない。

saved/current warningを分け、人間へ示す。保有quote/権利basis不足でNAVを作らず、既知factor比率が下限ならcoverageを示す。取引は[Ledger](portfolio-ledger.md)へ別記録する。

## 提案の訂正と人間の報告

非購入報告はexact提案への別記録でno_actionではない。本人の言葉・報告時点・対象時点と要約を分け、未発注/取消/未約定/永久却下を推測しない。[記録契約](../../engine/src/baibai_engine/investment_judgment/trade_proposal/record_contracts.py)に従い、提案/Review/台帳を変えない。

誤記録はexact参照/理由付き追記訂正で最新候補から除外する。残りは元の基準日・発行時刻・ID順で選び、関連旧提案への優先権や訂正時刻の再ランキングを作らない。原本/訂正はIDで読め、完了Operationも不変。保存表示/previewは同じ訂正集合、人間報告は別表示。訂正は新推奨/適格性回復ではなく、必要な当日refreshを省略しない。
