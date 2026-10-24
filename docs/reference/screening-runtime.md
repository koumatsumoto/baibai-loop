# Screening

L2の財務・倍率・機械見積りとMarket Contextを全対象のStock Analysisへ固定し、同じrunから候補文脈を作る。結果の組立はscreening/metric_builder、探索はdiscovery、較正はcalibrationが所有する。指標自体は[Valuation](valuation-metrics.md)・[Market Context](market-context.md)へ置く。

<a id="rule-purpose"></a>
## 探索の意味

入力scope、共通factの資格、Approach固有の推薦条件、有限容量、品質contextは異なる役割。いずれも企業の経済的rejectや購入採用を代行しない。完全なeligibility/order・値・method identityは[実装](../../engine/src/baibai_engine/screening/discovery/candidates.py)と[dated rules](../../method/screening/rules/)が正本である。

機械E[r]/FV、保存市場判断、portfolioは候補membership・順位を決めない。候補理由はApproach内rankで、AIのResearch選定理由とは別である。候補外の分析も調べられ、機械候補が全投資機会を表すとは扱わない。

<a id="candidate-discovery"></a>
## 四つの価値仮説

| Approach | 調査へ渡す問い |
| --- | --- |
| Current Earnings Power | 現在利益の反復性と業種割安の理由 |
| Normalized Earnings Power | 3FY平均が一期間の異常を離れた利益力を表すか |
| Asset Value | 非金融企業の資産が誰へ、いつ、どの費用で実現するか |
| Reinvestment Value | 売上成長が投資負担・利益率・資本収益を通して価値になるか |

金融業のcash/debt/securitiesへ非金融の資産proxyを適用しない。Reinvestmentのcapital returnは同一実績行の自己資本とEDINET debt/cashを使うproxyで、BS日が一致した厳密ROICではない。営業利益TTMを作れなければ累計利益や他利益で補わない。既知の0・負FCFも探索対象になり、欠測を0へ変えない。負FCFは成長投資の証明ではなくResearchの問いである。

各Approachのtop集合をexact unionにし、重複tickerは一行へ全理由を保持する。depthは有限資源の設定で、容量外を経済的棄却にしない。ticker順は非経済的なserialization順で、総合rank・Research priorityではない。比較母集団・丸め・tie-breakはproductionと較正で同じ純関数を使う。

## 入力不足と品質

普通株masterと価格を計算対象に使う。価格bar期間は利用可能な履歴proxyで正式上場日ではなく、短い履歴を長期財務・自己レンジの証拠にしない。現行の規模/期間floorとADVは低い値・欠損だけで共通資格を落とさず、各指標の必要履歴とJPX条件を別に維持する。

取得coverageは成功した要求範囲で、全銘柄の入力充足ではない。密度の分母から価格・財務欠測の普通株を消さず、計算不能を原本未取得とみなして再取得しない。nullと既知0、raw field欠落を保持する。TTM非exact・YoY欠測のpartial warningはrunの品質で、会社を一律除外するgateではない。

## 保存と読取

一回のrunがheader・全Stock Analysis・候補文脈を同じtransactionで確定する。独立した候補table・ID・後付け発行を持たない。保存revisionはその値・methodのまま読み、現在rulesで再計算しない。過去rulesを指定した新計算も、過去算式・当時値の再現とは限らない。

runのexit 2は保存済みpartial warning。DB成功後のfile export失敗では返されたrevisionから回収する。候補0件は正常で、古い候補runへfallbackしない。前runなしの日次差分は未測定とし、method/rulesが比較可能なrun間で差分を読む。

Researchの分析用選択はResearch日以下のanalysisとknown_at以下のrunを選ぶ。explicitの未知/未来revisionをlatestへ差し替えない。保存Research入力は元runのpruneから独立し、run pruneで評価・提案・固定snapshotを消さない。

## JPX決算予定と取得境界

[公式index](https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html)の日次資料と月別資料は対象が異なる。日次資料は予定なしでリンクがない場合もある。未定は不正値ではなく、同順位の矛盾をURL順で決めず、上位資料の未解決を下位の古い値で埋めない。優先順位はproviderの解決方針で公式規則とは称さない。

予定日到達は発表確認ではなく、個別Researchは会社IRを確認する。snapshot取得失敗は現行run全体を保存前に止めるが、個別tickerの予定なしとは別。この停止は入力・成果物の完全性の境界で、企業の除外理由や機会損失の実測ではない。

EDINETの取得・抽出失敗は、その実行結果として報告する。Screeningは最新の検証済み成功baselineから現行extractor・文書revisionと訂正／取下げ状態が適合する保存値だけを採用し、coverage検査も同じ選択を使う。当日抽出の成功は必須にしない。任意のEDINET値が未取得・不適格なら欠測として他項目を進めるが、破損した成功baseline／保存原本を古い成功で回避しない。元snapshot日・記録時刻はStock AnalysisとResearchのCF／BS入力へ保持し、提出日・対象期間やfactの測定日へ読み替えない。

保存入力の共通統合は、明示したasofも営業日判定し、非営業日は新しいScreeningを作らず既存runの日付・ID・計算時刻を保って統合・公開を進める。営業日のexact master・価格／財務coverageは引き続き必要。

通常runは保存済み入力だけを読む。取得・retry/deadline・成功chunkからの再開・発行順は[OPERATIONS](../../batch/OPERATIONS.md)に従う。意味を変えない配置変更でrules/model/calculation revisionを更新しない。
