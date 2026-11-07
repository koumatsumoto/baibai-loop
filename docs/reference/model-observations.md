# 観測と原典の読み方

情報層・領域名・保存責務は[architecture](../architecture.md)、機械候補は[Screening](screening-runtime.md)、仮定付き価値は[Stock Assessment](stock-assessment.md)、開始・再開は[OPERATIONS](../../jobs/OPERATIONS.md)を使う。

<a id="source-owners"></a>
## 原典・時点・不足

原公表機関か出所が分かる公式集約先、企業の一次開示、人間が確認したbroker事実を用途別に使う。報道は速報・地政学等の補助で、意見や数値の一次確認と分ける。source tierは正確さや利用許諾の保証ではなく、現運用では有料媒体を使わない。契約内の個人・非公開利用とし、原文・転載表を公開Issueへ再配布しない。

対象日・会計期、発表/訂正、実受領、入力cutoff、評価時刻は別である。未知日を受領日で埋めず、Web Archiveのcapture/closestも当時公表の証明にせず、原URL/captureを保つ。保存Generationの入力identityを使い、同じas-ofやlatest headで代用しない。固定snapshotは再開入力を保つが、未保持vintage・intradayを含む完全PITではない。後日訂正・収録範囲と当時既知の範囲を分ける。

未取得・取得失敗・取得済み空・partial・正常null・比較不能・0を区別する。新しい期や訂正の欠測を古い正常値で埋めず、不足は依存する値へ局所化する。tool障害を原資料の不存在としない。

一次API/CSV/索引にも継続障害があり、同じ期間・定義・値を複数経路で確認できた場合だけ、一次未確認を明示して二次配信を採る。単一値・不一致・一時障害で代用せず、AIの転記で観測storeを埋めない。実装・取得・公開・本番切替・計算可能を区別する。

source別取得範囲は[経済catalog](../../engine/src/baibai_engine/data_collection/catalog.yaml)・[取得owner](../../engine/src/baibai_engine/data_collection/acquisition/)へ。経済catalogは全L1の一覧ではない。算式・fieldは各モデル実装が正本である。

<a id="financial-periods"></a>
## 開示と会計比較

J-Quantsはnative HTTP pageがL1で、typed行から原本を作らない。完全codeと商品区分で普通株を識別し、末尾0・業種名から推定しない。calendarの元区分もbooleanから復元しない。JPXは選択完了bundleだけを投影し、旧行を混ぜず、消失を取消/不存在とせず、受領完了を同時公表としない。現在の決算予定から過去予定は復元できず、会社IRで再確認する。

[開示owner](../../engine/src/baibai_engine/machine_analysis/observations/financials/financial_documents.py)は値の有無より先に適用期・連結/単体・会計基準を決める。DiscNo、投入順、未知の同日時刻は訂正順でない。部分訂正/競合はfieldごとに扱い、空欄を撤回としない。過年度訂正で現在区分を戻したり、連結へ単体補足を混ぜたりしない。flagやL3要約の空一覧、修正再表示「無」だけで比較可能性・組替・除外を確定しない。基準未確定でも原額・独立した価格/株数/配当を残し、異基準の旧FYへ候補政策を迂回させない。時刻不明だけで変更のない観測を捨てない。

公表累計/FY、成立する単独四半期、年間/TTM、期末残高は別の量である。EPS・平均株数・営業CFを四半期差分へ変換せず、同じQ1や重複TTMを独立確認へ数えない。金額を合成して同じ普通株分母で最後に1株換算し、異なる平均株数のEPSを加減算しない。丸めやbasis不明のEPS×株数から総額を復元しない。

- **企業財務**: TTMは最新実績・前年FY・前年同期間を固定する。配当/予想だけの文書で実績を消さず、訂正欠測を旧版/別期で補わず、単一四半期を年率化しない。最新公表額による合成は企業成長の保守的な累計訂正制約と別契約。前年累計の配信がFYより後でも、対応資料の先行公表があり得る。
- **企業成長**: J-Quants実績summaryに会社予想を混ぜない。表示窓は会計期末であり、窓外の比較元/後日訂正を含む保持文書から計算する。窓変更で共通期の意味を変えず、読取から取得を起動しない。事業売却・M&A・為替・会計方針を同じ経済scopeへ組み替えた値ではない。J-Quantsが過去組替値を遡及供給するわけではなく、重要な組替はL3が原典から評価へ記録して原観測は保つ。
- **利益と倍率**: 報告利益/普通株時価総額のYは分配可能利益・期待収益率でない。連結は親会社帰属、単体は当期純利益で、優先配当等の調整は推定しない。FinancialSnapshotのEPSと会社の平均株数EPSを分ける。Forward PERは会社予想で外部consensusとは別。予想純利益/経常利益の大小や赤字注記だけで持続性・衰退を認定しない。
- **変化**: 赤字縮小/黒字化は元額と符号で読む。負/0基準の通常率だけで改善とせず、営業利益を経常利益で代用しない。金融業へ非金融の利益率を当てはめず、未検証株式basisの変化率を作らない。成長モデルはPER/peer比較を提供せず、別倍率や現在平均で不足を埋めない。

<a id="reported-comparisons"></a>
### 原本内の比較と組替

EDINETは同じmarket sealで文書・訂正・抽出revisionが適格な場合の任意入力で、未取得を全社生成の停止条件にしない。一覧/ZIP受領はL1、選択/metrics/segment/debtはL2。type=5 CSVとtype=1 XBRLは別経路で、抽出履歴から原本を作らず端末ZIPを暗黙採用しない。同じdocID/typeの最初のbytesを保ち、通常refreshは再利用する。再抽出と再取得は別で、提供元修復後の差替えは隔離取得・差分確認を伴う別操作で、自動修復を保証しない。

| 経路 | 成立範囲と限界 |
| --- | --- |
| [EDINET同一文書](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_comparisons.py) | 固定type=1 ZIPのIFRS年次/半期本表から当期・比較期を別系列へ保存する。金額・精度・期間・scope・原文位置を保ち、J-Quants TTM/倍率のoperandへ横結合しない。単体利益を親会社利益にせず、相対年度名から比較日を推定しない。 |
| [PDF組替](../../engine/src/baibai_engine/machine_analysis/observations/financials/pdf_selection.py) | 保持開示の最新期/訂正を照合し、同一基準の比較欄/TTMを組む。日本基準の遡及変更は同日公表の組替済み前期実績を要し、変更前FYで埋めない。表示丸めの近似と不明を残し、精密XBRL値と呼ばない。原hash・時刻・operandを保持する。 |

文書ごとの訂正系列を保ち、未取得の訂正を旧文書で埋めない。原本不足は独立したJ-Quants系列を落とさず、固定object破損は正常欠測にしない。[年次/半期fixture](../../tests/fixtures/edinet/comparisons/README.md)をQ1/Q3や全本表へ一般化せず、PDF受領をXBRL取得としない。未受領の訂正、金融業/業種未確認の利益率制約を組替成功で解除しない。

<a id="capital-basis"></a>
### 資本・価格・配当の単位

時価総額は同じ時点の発行済株数−自己株。平均株数を発行済総数、非支配持分込み純資産を普通株自己資本としない。TA×EqARは同一行を使い、独立carry値を掛けない。新しい資本状態に自己株等がなければ古い0/状態で補わず、処分/消却を推測しない。

期末株数と遡及分割EPS/BPSは異なる単位になり得る。[資本補正](../../engine/src/baibai_engine/machine_analysis/models/company_financials/metrics/capital.py)は独立株数・factor・利益/EPS・自己資本/BPSの裏付けが揃う場合だけ。数値一致で誤転記と決めず、前後と根拠を残す。JPX PDFで完了日/普通株総数を確認した転換にも受領時計/hashを付け、新発行済数から古い自己株数を引かない。有限原本を全社網羅としない。

大量保有報告の株数は第三者申告で、発行会社確認や変更日確定ではない。基準日・提出日・hashを保ち、時価総額/順位を自動補正しない。受領UTC日しか分からないとき、同日利用は評価前の固定snapshotに存在する場合だけとし、固定時刻を上限にする。他は翌UTC日以降に扱う。

[発行会社の資本注記reader](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_issuer_capital_reader.py)は固定一覧・原本の最新年次/訂正から、同じ普通株表の期末表示だけを読む。提出日contextの発行数や議決権表を接合せず、株/千株・連結範囲・原文位置を残す。信託・分割等の注記は未照合で、現在株数や価格分母には補完しない。未対応の表や最新訂正の欠測から旧数値を復活させない。

価格はraw closeに価格日より後のfactorを適用し、権利落ち当日は二重適用しない。別取得世代のprovider調整価格をつなげず、factor0を無eventにしない。rights issueを含む価格factorと、ExRTで確定した分割/併合だけの株数/1株値/配当換算を分け、価格から株数を逆算しない。

配当は予想/実績・期・basisを分ける。正の実績DPSには分割換算済み/原単位が混在し得る。期不明・逆転・未来終了・単位変更/未分類actionは未解決で、四半期末を基準日、総額÷期末株数を換算値にしない。factor相殺も変更を消さない。明示0/無配/未観測/basis未解決を区別し、未解決年度を飛ばして増配streakを復活させない。使える予想や原額は残し、旧機械carryを実現配当としない。

### EDINET金額を評価へ使うとき

[CSV抽出](../../engine/src/baibai_engine/machine_analysis/observations/financials/edinet_metrics/csv.py)は当期・基準・連結scope・context・JPYを選ぶ。明示0のASCIIハイフンと空欄/似た横棒、本表とsegment次元を区別し、連結値欠測を単体へ戻さない。JP/IFRS合算、独自conceptや類似名からの全額推定はしない。

- **cash/debt・証券**: 現預金と現金同等物を分け、金融債務抽出を全債務網羅としない。net cashは時点残高。証券は帳簿額で、含み損益・税・拘束・換金性・必要保有は未反映。非金融のasset-backed ratioはgross調査proxyで清算価値ではなく、金融業へ同じ解釈を適用しない。
- **EBITDA**: 同書類の営業利益と対応償却。異なるCF加算子を同義として落とさず、のれん償却で未知の減価償却を補わない。負EVと負EBITDAを分け、負倍率を割安としない。
- **OCF/capex**: 同context/scopeの合算と内訳を重複させず、支出符号を一度だけ正規化。年次120/130で取得した運転資金・設備支出の部分内訳は、Company Financialsの`edinet_reported_flows`へ同じ出典・scopeとともに渡す。OCF内訳はCFへの寄与符号、設備支出内訳は正の支出額で、欠測を0補完せず、合算CapExを推定分解しない。J-Quants OCFと混ぜてFCFを作らず、内訳0の感応度を正常FCF/維持投資としない。税・契約債務・必要資金を網羅する内訳ではなく、半期の取得対象も広げない。
- **期間/精度**: annual exactは年度metadataで、最新TTM・実測context期間・持続性の保証でない。半期・変則/不明年度を年率化せず、倍率不適格でも原額を残す。未保持context日を年度/提出日で補わず、短信OCF TTMとの精度差を残し、異期間のFCFとCFOを差し引かない。

短信との規模照合は範囲ずれ検知で、同時点/連結範囲の証明ではない。不適格を該当値へ局所化し、BS時点・debt scopeを開示日/期末/carry lagから推定しない。

<a id="edinet-research-facts"></a>
Company Research factsは直近年次/訂正・比較年度の任意dataset。売上内外・利益basis・連結・期・区分変更を揃え、独自conceptを名前で推定しない。提出JST日以前へ遡及せず、tickerだけでなく期/通貨を照合する。負債はJP GAAPの明示元本で社債括弧額を重複せず、リース・利率・担保・covenant・IFRS・5年超残差を網羅しない。抽出ok/0行を負債0とせず、失敗で以前のfactを消さず、将来CF/借換推定を混ぜない。

<a id="market-context-reads"></a>
## 価格・市場・業種の比較

各時点以下の最新masterの普通株を使い、除外銘柄を旧snapshotから戻さず、母集団を候補だけに縮めない。市場breadthはbenchmarkと同じ価格日。業種表はbenchmarkなしでも成立し得るがbreadth成功ではなく、各社bar窓の中央値は開始日が揃うとは限らない。個社相対はbenchmark/自己除外peerの両端日を揃え、補間や業種中央値で代用しない。ratio差0.1は10 percentage pointsで割安認定ではない。自己価格順位と旧倍率レンジ内位置も別。

<a id="technical-analysis"></a>
TAは配当を含まない調整終値。bar本数と共通営業日窓は別で、本数だけで鮮度・流動性・取引可能性を保証しない。価格のない日のactionも換算へ残す。MA/BB/RSIは同じ価格の加工で独立票ではなく、慣例の期間は最適性を証明しない。BBを95%包含/反転確率、低RSIをbuy/高RSIをsellとしない。Wilder RSIは履歴seed・訂正に依存し、flatの中立値は規約。12–1か月momentumは直近月を除く別窓である。

共通calendarの端点returnと実在最高終値peakからの下落を区別する。peakは未観測日の最高値保証でなく、長期下落は前年比returnではない。価格なしでは通常無約定/終日停止を識別できず、row不在/部分欠測/不正値も分ける。実在価格対が注記成立を示す場合と、未知日が不成立を確定できなくする場合があり、欠測は一律陽性取消でも陰性でもない。

価格注記は研究経路の仮の目安で、採否・最適閾値・自動開始・追加調査義務ではない。観測方法と注記政策を分け、TAとMCの`price_path_relative`は同じ実在端点で比較する。benchmark不足は相対値だけへ局所化し、旧MCのbar窓/高値や旧run候補母集団を現行値へ再計算しない。

<a id="margin"></a>
### 信用需給

全銘柄週次/日次と選別subsetのmargin-alertは別母集団。非掲載を0・全銘柄欠測の補完にしない。PubDateは公表日、AppDate/日次Dateは残高日で規制開始でない。公表前の空/失敗/対象なしを分け、金額欠測を株数×価格で作らない。

[reader](../../engine/src/baibai_engine/machine_analysis/observations/trading_activity/margin_reader.py)は明示公表日を優先し、なければ週次2/日次1営業日lagを使う。公表当日は含めず、実配信時刻の証明とはしない。株数/出来高は確定分割/併合だけで揃え、換算不能は依存比率/差へ局所化する。同一残高のlong/short比まで消さず、残高÷平均出来高を解消日数予測としない。

<a id="reading"></a>
## 経済Reading

[固定観測とdated規則](../../engine/src/baibai_engine/machine_analysis/generation/runners/market_reading.py)から水準・分布・鮮度を記述する。regime/合成score/売買signalにせず、L2系列を原本storeへ複製しない。水準/前年比のstatisticと比較窓・観測数・samplingを読み、履歴不足・stale・取得失敗・分布端を分ける。latestは保存最終観測で今日の値ではなく、公表見込みはcadence/lag推定で公式calendarでない。

受領履歴と公表vintageを分け、latest空/欠測を旧値で埋めず、暦日を人工補完しない。`recomputed_from_current_store`は後日取得/改訂込みでResearchのknown_atを適用しない。公表vintageを持つsourceだけ対象日で制限し、bulk受領時計で当時既知の履歴を一律に落とさない。

| 原典/観測 | 固有の意味 |
| --- | --- |
| FRED/H.15 | rolling/開始日で範囲が限られ、CSV先頭は全歴史の保証でない。変更受領と同値確認の時計は[運用](../../jobs/OPERATIONS.md#経済原本)へ。 |
| CFTC/ECB | long/short・通貨両脚を保ち、net/crossから原値を復元しない。 |
| e-Stat | 頻度・季調・地域を揃え、確報欠測を速報で埋めない。単位codeだけで基準年を決めない。 |
| BOJ/財務省/投資部門別 | 期間代表日・集計末と公表/訂正cutoffを分ける。 |
| Yahoo close/流動性 | 日足は途中値を含み、保存session/受領時計で読む。session終了/過去barも公式終値/先物清算値の保証ではない。WALCL水曜・WTREGEN週平均・日次RRPのネット流動性は同時点残高/投資可能cashでない。 |
| multpl/松井 | 月次推定表示を日々の過去currentへ復元しない。松井は同社信用買建だけで全市場/追証人数でなく、価格下落の独立票にしない。符号を紹介算式で直さず、年推定や再掲を公表年/鮮度の証明としない。 |

<a id="option-iv"></a>
IVの満期/30日補間は[計算owner](../../engine/src/baibai_engine/machine_analysis/observations/economic_indicators/option_iv.py)。SQ直後の欠測を平穏、term負値をパニック、skew縮小を警戒後退、put/call差をsource誤りと断定しない。緊急証拠金日は指定部の清算値を使う。[過去の診断](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-07-31-option-iv-quantiles/report.md)は売買閾値/収益の検証ではない。

<a id="external-assessment"></a>
## 外部評価と記事

モデル価格・予想/rating・Quants scoreを分け、origin・対象期・単位・株式basis/尺度を揃えた差だけ読む。初回・欠測回復・人数変更・再受領を改善とせず、転載を独立票にしない。保存最新値は現在値/最新試行成功ではなく、受領間比較は自己評価との比較を保証しない。投稿/総合診断をアナリスト予想にせず、人数・年表示・過去列から未記載の期末/EPS/履歴を作らない。銘柄同一性がない404/「情報なし」を正常欠測にしない。

<a id="tradingview"></a>
公開HTMLのbody/encodingとTradingView SDK応答は保存可能なnative表現でwire bytesではない。楽天outer/iframeは同試行、403/429は迂回しない。

TradingViewの受領JST日/master日を公表日、休日同値を独立sample、部分公開を市場代表としない。metric別の通貨・単位・FY/next FY・人数を確認し、一括流用しない。実受領cutoff/次取引機会を守り、参考closeを株価の第二正本にしない。

<a id="public-articles"></a>
索引・本文受領・投影・主張抽出は別進捗。見出し/概要/全文/部分の範囲・欠落を残す。BBC World/Business、Fed/ECB通常取得はRSS見出し/概要までで、重複feedを独立source、発言を政策決定としない。本文実読/判断接続の受入は[#1705](https://github.com/koumatsumoto/baibai-loop/issues/1705)に残る。金利CSVの文字表現は経済系列の独立測定でなく、XLSXセル投影は図表/書式/計算結果の復元ではない。[exact回復](../../jobs/OPERATIONS.md#article-acquisition)は保存原本を使う。

<a id="judgment-use"></a>
## 判断への受渡し

Selectionは固定Generationの必要結果を持ち、決算予定・規制・現在台帳まで含むとは限らない。表示の拡大は原本保持/生成範囲/入力hashを増やさず、保存モデルの範囲不足を補わない。固定旧入力から別Generationを保存する専用CLIは未提供で、現在marketの公開jobを代用しない。受入fixture/有限標本を全市場の原典照合や収益優位へ一般化しない。

外部評価は利益・倍率・資本・期の重要差だけ原典確認し、銀行のEBITDA/FCF等を全社必須にしない。rating加点・coverage別laneは設けない。[固定8社での不採用](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-22-decision-quality-closure/report.md)は将来効果の否定でない。Investing.comは変更の補助参照で、第二の日次consensusにしない。

<a id="public-research-reports"></a>
[SRC](https://holistic-r.org/report/)・[フィスコ](https://www.fisco.co.jp/service/report/)は選定企業の理由/仮説の補助。毎回取得/coverageや日次crawlerを要求しない。独自予想と会社計画、sponsor調査と独立根拠を分け、銘柄・発行/訂正・対象期・実読ページ・採用HTTPS原本を確認する。snippetを本文実読とせず、届かなければ一次資料へ戻る。

保有quoteはJ-Quantsを使い、公開quoteを台帳転記して埋めない。補助値には時点/basisを示す。Outcomeの比較は[Portfolio Ledger](../../.agents/skills/ledger-record/SKILL.md#historical-outcome)へ、世界経済の解釈・発行履歴は[市場判断skill](../../.agents/skills/market-context-assessment/SKILL.md)へ進む。
