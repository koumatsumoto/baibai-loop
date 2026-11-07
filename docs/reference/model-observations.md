# 観測と原典の読み方

層・保存は[architecture](../architecture.md)、候補は[Screening](screening-runtime.md)、価値判断は[Stock Assessment](stock-assessment.md)、操作は[OPERATIONS](../../jobs/OPERATIONS.md)へ。ここは観測の解釈を扱い、算式/fieldは実装を正本とする。

<a id="source-owners"></a>
## 原典・時点・不足

全モデルで次を共通に守り、後続節ではsource固有の差だけを扱う。

- **原典**: 原公表機関・出所が分かる公式集約先・企業開示・確認済みbroker事実を用途別に使う。報道は速報等の補助。source tierは正確さ/許諾を保証せず、有料媒体は使わない。契約内の個人・非公開利用に限り、原文/転載表を公開Issueへ再配布しない。
- **時計**: 対象日/会計期、公表/訂正、実受領、入力cutoff、評価時刻を分ける。未知日を受領日で埋めず、Web Archiveも当時公表の証明にせず原URL/captureを保つ。保存Generationの入力identityをas-of/latest headで代用しない。固定snapshotは未保持vintage/intradayまで復元する完全PITではない。
- **比較**: 期間・会計scope・通貨/単位・株式basisを揃え、原値・定義・時計・参照を保つ。同じ原値の加工/転載/再受領は独立証拠でなく、有限fixture/coverageは全市場・将来収益の実証でない。後日訂正と当時既知を分ける。
- **不足**: 未取得・失敗・取得済み空・partial・正常null・比較不能・明示0を区別する。新期/訂正の不足を旧版・別期・別basisで埋めず、依存値だけへ局所化する。tool障害を不存在、固定object破損を正常欠測にしない。派生値が使えなくても原額/独立観測は残す。
- **代替**: 継続障害時も、期間・定義・値が複数経路で一致した場合だけ一次未確認を明示して二次配信を採る。単一値・不一致・一時障害で代用せず、AI転記でstoreを埋めない。実装・受領・公開・切替・計算可能は別の到達点。

[経済catalog](../../engine/src/baibai_engine/data_collection/catalog.yaml)は経済の取得範囲で全L1一覧ではない。他sourceは[取得owner](../../engine/src/baibai_engine/data_collection/acquisition/)を参照する。

<a id="financial-periods"></a>
## 開示と会計比較

普通株は[J-Quants原本](../../jobs/OPERATIONS.md#jquants-native-operations)の完全code/商品区分で識別し、末尾0/業種名から推定しない。calendar元区分もbooleanから復元しない。JPXは選択完了bundleを使い、旧行を混ぜず、消失を取消/不存在、受領完了を同時公表としない。過去の決算予定は会社IRで再確認する。

[開示選択](../../engine/src/baibai_engine/machine_analysis/observations/financials/financial_documents.py)は値より期/連結区分/基準を優先する。訂正/競合はfield単位で、DiscNo・投入順・未知時刻を順序に使わない。空欄を撤回、過年度訂正を現在区分変更とせず、時刻不明だけで不変観測を捨てない。flag/要約空一覧/修正再表示「無」では比較可否を決めず、基準未確定を旧FYへの迂回で補わない。

累計・単独四半期・TTM・期末残高は別。EPS/平均株数/営業CFを四半期差分にせず、金額合成後に普通株分母で換算する。異分母EPSの加減算や丸め/basis不明EPS×株数の総額復元はしない。

- **財務/成長の違い**: 財務TTMの最新額合成と成長の累計訂正制約は別契約。配当/予想文書で実績を消さず、予想混入/単一四半期の年率化はしない。前年累計の配信がFYより後でも原資料は先行し得る。
- **成長の窓**: 会計期末の表示窓外の比較元/訂正も使い、窓変更で共通期の意味を変えず読取から取得しない。売却/M&A/為替/会計方針を組替済みの有機成長とは呼ばない。組替値の遡及供給は保証されず、重要差はL3が原典で評価し原観測を保つ。
- **利益/倍率**: 報告利益のYは分配可能利益/期待収益率でない。連結は親会社帰属、単体は当期純利益で優先配当等は推定しない。FinancialSnapshot EPSと会社平均株数EPS、会社予想とconsensusを分け、予想純利益/経常利益の大小や赤字注記だけで持続性を決めない。
- **変化**: 赤字縮小/黒字化は額と符号で読み、負/0基準の率で改善を決めない。営業利益の経常利益代用、金融業への非金融利益率適用、未検証basisの変化率を避け、成長モデルが持たないPER/peer比較で補わない。

<a id="reported-comparisons"></a>
### 原本内の比較と組替

EDINETは同じmarket sealの文書/訂正/revisionに適格な任意入力で、未取得で全社生成/J-Quantsを止めない。type=5 CSVとtype=1 XBRL、L1受領とL2抽出を分ける。[原本保持/再開](../../jobs/OPERATIONS.md#edinet-list-native-l1)に従い初回bytesを使う。再抽出は再取得でなく、提供元修復後の差替えにも隔離取得/差分確認が要る。

| 経路 | 固有の成立範囲 |
| --- | --- |
| [EDINET同一文書](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_comparisons.py) | 固定type=1のIFRS年次/半期本表の当期・比較期を別系列へ保存し、金額/精度/期間/scope/原文位置を保つ。J-Quants TTM/倍率のoperandへ横結合せず、単体利益を親利益、相対年度名を比較日に読み替えない。 |
| [PDF組替](../../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_selection.py) | 保持開示の最新期/訂正を照合した同一基準の比較欄/TTM。日本基準の遡及変更は同日公表の組替済み前期実績を要する。表示丸めの近似/不明と原hash/時刻/operandを保ち、精密XBRL値とは呼ばない。 |

訂正系列を保ち、組替成功でも未受領訂正や金融業/業種不明の利益率制約を解除しない。[fixture](../../tests/fixtures/edinet/comparisons/README.md)の年次/半期をQ1/Q3・全本表へ、PDF受領をXBRL取得へ一般化しない。

<a id="capital-basis"></a>
### 資本・価格・配当の単位

発行済総数/平均株数、普通株自己資本/非支配持分込み純資産を分ける。時価総額の自己株控除は同じ資本状態、TA×EqARは同一行を使う。旧状態で新しい自己株欠測を補い処分/消却を推測しない。

期末株数と遡及分割EPS/BPSの混在は[資本補正](../../engine/src/baibai_engine/machine_analysis/models/company_financials/metrics/capital.py)の裏付けで扱い、数値一致だけで誤転記にしない。JPX PDFの転換も完了日/普通株総数/受領時計/hashを確認し、新発行済数と旧自己株を混ぜない。

大量保有報告は第三者申告で、発行会社確認/変更日確定でない。基準日・提出日・hashを保ち、時価総額/順位を自動補正しない。受領UTC日しかなければ翌日以降に扱い、評価前の固定snapshotにある場合だけ同日利用をその固定時刻まで認める。

[発行会社の資本注記reader](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_issuer_capital_reader.py)は固定一覧・原本の最新年次/訂正から、同じ普通株表の期末表示だけを読む。提出日contextの発行数や議決権表を接合せず、株/千株・連結範囲・原文位置を残す。信託・分割等の注記は未照合で、現在株数や価格分母には補完しない。未対応の表や最新訂正の欠測から旧数値を復活させない。

[価格換算](../../engine/src/baibai_engine/machine_analysis/observations/prices/price_basis.py)はraw closeを同じ基準へ揃え、別取得世代の調整価格をつながない。rights issueを含む価格調整と、確定分割/併合だけの株数/配当換算を分け、価格から株数を逆算しない。

実績DPSの原単位/分割換算を確かめ、期不明・逆転・未来終了・不明actionを解決済みにしない。四半期末を基準日、総額÷期末株数を換算値とせず、factor相殺で変更を消さない。無配と未解決を分け、欠けた年度を飛ばして増配streakを復活させない。予想/原額は残し、旧carryを実現配当としない。

### EDINET金額を評価へ使うとき

[CSV抽出](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_metrics/csv.py)の基準/context・0/空欄・合算/内訳を使い、本表とsegment、JP/IFRSを混ぜず、独自concept/類似名から全額を推定しない。

- **残高**: 現預金と現金同等物を分け、抽出債務を網羅としない。証券は税/拘束/換金性等未反映の帳簿額。net cashは時点残高、非金融のasset-backed ratioはgross調査proxyで清算価値でなく、金融業へ流用しない。
- **EBITDA**: 同書類の対応償却を使い、異なるCF加算子の省略や、のれん償却による減価償却補完をしない。負EV/負EBITDAを分け、負倍率を割安としない。
- **OCF/capex**: 年次120/130で取得した運転資金・設備支出の部分内訳は、Company Financialsの`edinet_reported_flows`へ同じ出典・scopeとともに渡す。OCF内訳はCFへの寄与符号、設備支出内訳は正の支出額で、欠測を0補完せず、合算CapExを推定分解しない。J-Quants OCFと混ぜてFCFを作らず、内訳0の感応度を正常FCF/維持投資としない。税・契約債務・必要資金を網羅する内訳ではなく、半期の取得対象も広げない。
- **期間**: annual exactは年度metadataで最新TTM/実測context/持続性の保証でない。半期・変則/不明年度を年率化せず、context日を年度/提出日で補わない。短信OCF TTMとの精度差を保ち、異期間FCF/CFOを差し引かない。

短信との規模照合は範囲ずれ検知で同時点/連結範囲の証明でなく、BS時点/debt scopeも開示日・期末・carry lagから推定しない。

<a id="edinet-research-facts"></a>
Company Research factsは年次/訂正と比較年度の任意入力。外部/内部売上・利益basis・区分変更を揃え、提出JST日以前へ遡及しない。JP GAAPの明示元本は社債括弧額を重複せず、リース/利率/担保/covenant/IFRS/5年超残差を網羅しない。抽出ok/0行を負債0とせず、失敗で既存factを消さず、CF/借換推定を混ぜない。

<a id="market-context-reads"></a>
## 価格・市場・業種の比較

各時点以下の最新masterの普通株を使い、除外銘柄を旧snapshotから戻したり母集団を候補へ縮めたりしない。市場breadthはbenchmarkと同じ価格日。業種表はbenchmarkなしでも成立し得るがbreadth成功でなく、各社bar窓の中央値は開始日共通とは限らない。個社相対はbenchmark/自己除外peerの両端日を揃え、補間/業種中央値で代用しない。ratio差0.1は10 percentage pointsで割安認定ではなく、自己価格順位と旧倍率レンジ位置も別。

<a id="technical-analysis"></a>
TAは配当なしの調整終値で、bar本数だけでは窓・鮮度・流動性・取引可能性を保証しない。慣例期間の最適性、BBの95%包含/反転確率、低/高RSIのbuy/sellを仮定しない。Wilder RSIは履歴seed/訂正に依存しflat中立値は規約。12–1か月momentumは直近月を除く。

共通calendar端点returnと実在最高終値peakからの下落は別で、未観測日の最高値保証/前年比returnとしない。価格なしでは無約定/終日停止を識別できず、row不在/部分欠測/不正値を分ける。実在価格対で注記成立を示せても未知日が不成立確定を妨げ得るため、欠測を一律陽性取消/陰性にしない。

価格注記は仮の研究目安で、採否/自動開始/調査義務でない。TA/MCの`price_path_relative`は実在端点を揃え、旧MC窓/高値や旧run母集団を再計算しない。

<a id="margin"></a>
### 信用需給

全銘柄週次/日次と選別subsetのmargin-alertは別母集団。非掲載を0・全銘柄欠測の補完にしない。PubDateは公表日、AppDate/日次Dateは残高日で規制開始でない。公表前の空/失敗/対象なしを分け、金額欠測を株数×価格で作らない。

[reader](../../engine/src/baibai_engine/machine_analysis/observations/trading_activity/margin_reader.py)は明示公表日を優先し、なければ週次2/日次1営業日lagを使う。公表当日は含めず、実配信時刻の証明とはしない。株数/出来高は確定分割/併合だけで揃え、換算不能は依存比率/差へ局所化する。同一残高のlong/short比まで消さず、残高÷平均出来高を解消日数予測としない。

<a id="reading"></a>
## 経済Reading

[固定観測とdated規則](../../engine/src/baibai_engine/machine_analysis/generation/runners/market_reading.py)から水準・分布・鮮度を記述する。regime/合成score/売買signalにせず、L2系列を原本storeへ複製しない。statisticの水準/前年比・比較窓・観測数・samplingを読み、履歴不足/stale/取得失敗/分布端を分ける。latestは保存最終観測で、公表見込みもcadence/lag推定であり公式calendarでない。

暦日を人工補完しない。`recomputed_from_current_store`は後日取得/改訂込みでResearchのknown_atを適用しない。公表vintageを持つsourceだけ対象日で制限し、bulk受領時計で当時既知の履歴を一律に落とさない。

| 原典/観測 | 固有の意味 |
| --- | --- |
| FRED/H.15 | rolling/開始日の限界があり、CSV先頭≠全歴史。変更受領/同値確認の時計は[運用](../../jobs/OPERATIONS.md#経済原本)へ。 |
| CFTC/ECB | long/short・通貨両脚を保ち、net/crossから原値を復元しない。 |
| e-Stat | 季調/頻度/地域を揃え、確報を速報で埋めず、単位codeから基準年を決めない。 |
| BOJ/財務省/投資部門別 | 期間代表日/集計末≠公表/訂正cutoff。 |
| Yahoo close/流動性 | 日足には途中値があり、session/受領時計を読む。終了/過去bar≠公式終値/先物清算値。水曜WALCL・週平均WTREGEN・日次RRPのネット流動性≠同時点残高/投資可能cash。 |
| multpl/松井 | 月次推定を日々の過去currentへ戻さない。松井は同社信用買建だけで全市場/追証人数でない。紹介算式で符号を直さず、年推定/再掲を公表年/鮮度の証明としない。 |

<a id="option-iv"></a>
IVの満期/30日補間は[計算owner](../../engine/src/baibai_engine/machine_analysis/observations/economic_indicators/option_iv.py)へ。SQ直後の欠測≠平穏、term負値≠パニック、skew縮小≠警戒後退、put/call差≠source誤り。緊急証拠金日は指定部の清算値を使う。[過去診断](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-07-31-option-iv-quantiles/report.md)は売買閾値/収益を検証していない。

<a id="external-assessment"></a>
## 外部評価と記事

モデル価格・予想/rating・Quants score・投稿/総合診断を分け、origin/尺度を揃える。初回/欠測回復/人数変更は改善でなく、保存最新値は現在値/最新試行成功でない。受領間比較を自己評価との比較とせず、人数/年表示/過去列から未記載の期末/EPS/履歴を作らない。銘柄同一性のない404/「情報なし」は正常欠測でない。

<a id="tradingview"></a>
HTML/encoding・SDK応答は保存native表現でwire bytesではない。楽天outer/iframeは同試行、403/429は迂回しない。取得/再開は[外部評価](../../jobs/OPERATIONS.md#external-assessment-acquisition)・[TradingView](../../jobs/OPERATIONS.md#tradingview-expectations)へ。

TradingViewの受領JST日/master日を公表日、部分公開を市場代表としない。metric別の通貨・FY/next FY・人数を一括流用せず、実受領cutoff/次取引機会を守る。参考closeは株価の第二正本でない。

<a id="public-articles"></a>
索引/本文受領/投影/主張抽出は別進捗。見出し/概要/全文/部分の範囲を保ち、BBC World/Business・Fed/ECB通常取得はRSS見出し/概要まで、発言≠政策決定。本文実読/判断接続の受入は[#1705](https://github.com/koumatsumoto/baibai-loop/issues/1705)へ。金利CSVの文字表現は独立測定でなく、XLSXセル投影は図表/書式/計算結果を復元しない。[exact回復](../../jobs/OPERATIONS.md#article-acquisition)は保存原本を使う。

<a id="judgment-use"></a>
## 判断への受渡し

Selectionは決算予定/規制/現在台帳を網羅するとは限らず、表示拡大では保存入力/生成範囲も増えない。固定旧入力から別Generationを保存する専用CLIは未提供で、現在market公開jobを代用しない。

外部評価は利益/倍率/資本/期の重要差を原典確認し、銀行のEBITDA/FCF等を全社必須にしない。現行ではrating加点/coverage別laneを設けない。[固定8社での不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-22-decision-quality-closure/report.md)は将来効果の否定でない。Investing.comは変更の補助で第二の日次consensusにしない。

<a id="public-research-reports"></a>
[SRC](https://holistic-r.org/report/)・[フィスコ](https://www.fisco.co.jp/service/report/)は仮説の補助で毎回取得/日次crawlerを要求しない。[根拠の確認](stock-assessment.md#観測と根拠)に従い、独自予想/会社計画、sponsor調査/独立根拠を分け、実読ページと採用HTTPS原本を残す。届かなければ一次資料へ戻る。

保有quoteはJ-Quantsを使い公開quoteを台帳転記で埋めない。補助値は時点/basisを示す。Outcomeは[Ledger](../../.agents/skills/ledger-record/SKILL.md#historical-outcome)、世界経済の解釈/発行履歴は[市場判断skill](../../.agents/skills/market-context-assessment/SKILL.md)へ。
