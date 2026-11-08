# Trade Proposal

[exact評価pair](stock-assessment.md#independent-second-pass)を確認済み価格・cash・保有/注文へ適用する。未評価の重要材料は評価/Reviewへ戻す。公開と採用/予約/注文/約定は別で、操作は[skill](../../.agents/skills/investment-judgment/SKILL.md)へ。

## 行動と比較

| 行動 | 意味 |
| --- | --- |
| buy | 作者の上限で買う指値提案。単なる監視価格ではない。 |
| hold | [確認済みの保有継続](#holding)。 |
| sell | [確認済み理由による全量回収](#holding)。 |
| no_action | 完成した経済比較による見送り。 |
| defer | 調査・入力・判断条件の不足。 |

固定Operation全対象をnew/holding別に被覆する。未調査はunfinishedの理由/参照に対応する評価参照なしdefer、Review済みuncertain/unresolvedは参照付きdeferにできる。0対象に空提案は作らない。

[経済性](../doctrine.md#economic-return-policy)を指値・数量・待機案で比べ、採用理由/見直し条件へ結ぶ。同じBase年率でも倍率上昇/利益回復への依存は異なり、Pmax内だけで採用しない。

## 条件付き購入

quoteは未調整終値で売気配ではない。評価/権利basisとcashを確認し、作者limit・実約定と分ける。許容価格域の説明は任意で、架空quoteや追加gateを作らない。

`place_limit`は[上限以下で買う指値](https://www.jpx.co.jp/glossary/sa/174.html)。quote超も可能だがstop/自動上乗せではなく、上限支払やquote近辺の約定を保証しない。価格分類は[提案契約](../../engine/src/baibai_engine/investment_judgment/trade_proposal/contracts.py)、呼値は[grid owner](../../engine/src/baibai_engine/portfolio/holdings/price_grid.py)が持つ。

PmaxはBase・期間・[要求率](../doctrine.md#required-return-policy)からの上限で、期待値/勝率/最適指値ではない。上振れでlimitのBase年率未達を通さない。quoteがPmax超でも低いlimitで待てるが、判断未了の監視はbuyにしない。

[受容理由](stock-assessment.md#uncertainty-and-decision)のあるsupported/resolvedを使う。[no-add](../doctrine.md#investment-strategy)を守り、active買予約への追加や約定済み提案の全売却後再利用もしない。

<a id="limit-selection"></a>
### 指値と待機案の選択

見返り・cash/単元・既知イベントで判断が変わる少数案を現金維持/他案と比べ、採用理由・重要差・反転条件を残す。全呼値探索や件数ノルマは不要。

[同context](../../engine/src/baibai_engine/investment_judgment/trade_proposal/inputs.py)と購入算術を使い、鮮度は実quote、支出/利回りは作者limit、価格/株数basisはsnapshotへ揃える。入力変更時は比較を取り直す。

[資本方針](../doctrine.md#required-return-policy)に従い、小口・上限量・現金維持の損益金額/残cashを比較する。同じ一株returnでも集中・ADV・no-addの放棄利益は異なる。

context/check/previewは同じ未拘束cashで指値全量成立する条件例で、数量未指定の上限量は採用量でない。[分配/税費用の算術](stock-assessment.md#scenario-arithmetic)に従い残cash/現金維持は無利息。全体NAV/約定予測ではなく、待機後の買付・cash利回り・費用が採否を変えるなら同一終期の別案へ反映する。

待機では逃す利益と保持cash・他機会・回避損失・追加情報を比較する。悪材料時の逆選択/event gapも考え、約定率×Base利益だけで選ばない。価値織込済みの毀損をpenaltyへ重ねず、機会損失を事後最高値としない。ADV/日足から板・queue・impact・精密約定率を創作せず、`low <= limit`も約定保証にしない。確率には[根拠・幅・感応度](../doctrine.md#fact-analysis-separation)を示す。

session・前提/イベントと未約定・部分約定・新情報後の確認時点を残す。変更は[再Review](#publication-review)へ。部分約定後は実保有・残注文・cash/no-addで判断し、残数再提案・指値引上げ/延長・上限超追随を自動化しない。

<a id="price-grid"></a>
### 具体条件

価格・数量・session・前提を保存する。共通gridはJPX「その他」の保守案でTOPIX500最良呼値/注文受理を保証しない。分類/確認期間等のterms不足は経済判断と分け、limitを後日quoteで補わず価格/数量未確定を完成としない。

session/見直し時点はbroker期限でなく、実期限は人間確認の取引事実。[価格・時間優先と板寄せ/ザラバ](https://www.jpx.co.jp/equities/trading/domestic/04.html)で結果が変わり、即時約定も保証しない。

<a id="holding"></a>
## 保有継続と回収

当初理由をexact参照で読み、今回の重要な一次根拠で確認する。

- 重大なbreakはsellを優先。quote/valuation/数量basis不足はterms不足で、holdへ戻さない。
- intactは旧valuation/quote欠測でもhold可だが、当日の十分な見返りとは別。将来の幅だけで取消さない。
- 見返り十分/不足には同日起点valuationと現在算術が必要。仮説未評価はdefer。

残存見返りは今後保有する増分。受取済みcash、今売っても得る確定済み未入金配当を再算入せず、旧horizonの割直しで水増ししない。税・費用・遅延・下振れが結論を変えるなら扱う。

sell limitは作者の一株回収下限。実quoteの上下とも可能だがgridに合わせ、黙って切り下げない。全量notionalは税費用前の回収下限で予想約定額/確定cashではない。下限回収・高値待ち・継続と未約定後の再確認を記す。

`remaining_reward` sellは実quoteでの不足に加え、同評価・期間・今後分配を作者下限で保有する機会費用（`sell_limit_projection`）も比べる。観測quote/売却後収益ではない。下限で保有が魅力的なら下限引上げ/hold/再確認と比較してなお回収する理由をReviewする。自動Pmin・Pmax sell gate・現在十分なのにTarget到達時だけ売る条件を作らず、broken sellには無効valuationの下限算術を課さない。

価格下落・期間経過・回復遅延・Target到達・新規購入条件未達・他候補/集中warning単独では売らず、次候補も必須にしない。全量sellの端株/不明数量は丸めない。active予約の注文処理を別確認し、人間報告を台帳反映してtermsを再確認する。

<a id="publication-review"></a>
## Review・保存・preview

別作業者の内容Reviewを正規checkの`draft_sha256`へ束縛し、valid/file hashで代用しない。内容変更は再Review、入力事実変更はsnapshotも取り直す。読取時刻差だけを事実変更にせず、検証は実時刻、公開はエンジンの初回発行時計を保つ。

公開時に固定scope・exact評価pair・現入力を照合する。previewは保存案と現在再試算を分け、旧保存を下限比較済みとせず、現在上限超でもlimit/数量を書換えない。利用不能を当時の誤りと即断せず、過去本文/hashへ現在条件を適用しない。

saved/current warningを分け、人間へ示す。保有quote/権利basis不足でNAVを作らず、既知factor比率が下限ならcoverageを示す。取引は[Ledger](../../.agents/skills/ledger-record/SKILL.md)へ別記録する。

## 提案の訂正と人間の報告

[非購入報告](../../engine/src/baibai_engine/investment_judgment/trade_proposal/record_contracts.py)はexact提案への別記録でno_actionではない。本人の言葉・報告/対象時点・要約を分け、未発注/取消/未約定/永久却下を推測せず、提案/Review/台帳を変えない。

誤記録はexact参照/理由付き追記訂正で最新候補から除く。残りは元の基準日・発行時刻・ID順で選び、関連旧提案の優先や訂正時刻の再順位付けをしない。原本/訂正はIDで読め、完了Operationは不変。保存表示/previewは同じ訂正集合、人間報告は別表示とし、新推奨/適格性回復や当日refreshの代わりにしない。
