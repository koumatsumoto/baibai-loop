# データソース

数値・経済統計は原公表機関または出所を確認できる公式集約先、企業調査は一次開示、broker事実は人間の確認報告を使う。速報・地政学の補助はReuters・NHK・AP等の事実報道とし、意見記事と数値の一次確認を混同しない。有料媒体は現行運用では使わない。sourceのtierは用途であり、正確さや利用許諾の点数ではない。

取得と原本はData Collection、選択・単位変換・計算はL2が所有する。取得toolの障害を原資料の不存在にせず、同じ公表元のAPI・CSV・索引を調べる。異なる時刻・定義のsourceを無説明に接続しない。主な取得先と能力は既存source catalog・provider、L2の列と式は各領域の定義が正本である。

## 入手不能と利用条件

一次統計の二次配信は、公式経路を確認してもsource側の継続的な問題で解決できず、同じ期間・定義・数値を複数経路で確認できた場合だけ一次未確認と明示して使う。失敗した一次inputと実読した二次inputを分け、判断は採用したsourceへ結ぶ。単一値・不一致・toolの一時障害では代用せず、AIが観測storeへ転記しない。Tier外の速報補助もその旨を示す。

Web Archiveのcapture時刻は元資料の公表時刻ではなく、closestもcutoff以前を保証しない。原URL、公表日、archive URLとcapture時刻を分け、改定前内容を確定できなければ未確認とする。

取得資料は契約・利用条件の範囲で個人・非公開運用に用いる。この文書は利用許諾を拡張せず、原本全文・転載表を公開Issueへ再配布しない。保存場所は[architecture](../architecture.md#store-authority)、固定releaseの操作は[OPERATIONS](../../jobs/OPERATIONS.md#fixed-release-read)に従う。

## J-QuantsとJPX

J-Quantsはnative HTTP pageをL1として保存する。masterや財務summaryのtyped行は選択・正規化後のL2であり、HTTP bytesの代わりではない。未使用field・空欄・当期/翌期slotを原本に残し、旧flattened行から架空の原本を作らない。取得済み空と未取得・partial、件数とcoverageを区別する。

masterは完全な提供元codeとrecordを保持し、主銘柄codeと商品区分を組み合わせて国内普通株を識別する。末尾0だけではETF/REITも含み、業種名から商品を推測しない。市場の選択は普通株判定とは別である。calendarは提供元区分を保持し、現物営業日を導出する。古いbooleanから区分を復元しない。

財務のDiscNoは文書identityでありrevision順序ではない。開示日時と会計期間を使う。訂正前情報や取得前の公開範囲を持たない期間を完全PITとしない。共通marketが原本文書を読み、[企業価値](company-valuation.md)が現在評価の予想・実績を、[企業成長](company-growth.md)が過去実績の状態・比較期間を選ぶ。

JPXの規制・決算予定・資本政策・上場廃止は完了済みsource bundleの原本からL2へ投影する。bundle完了は宣言対象の受領で、同時公表snapshotの保証ではない。選択bundleにない旧月・旧行を合流させず、消えた行を取消や不存在の証明にしない。現在取得した決算予定だけで過去の予定履歴を再現せず、個別調査では会社IRを再確認する。

### 信用残のsourceと日付

| source | 母集団と残高対象日 |
| --- | --- |
| margin-interest旧週次 | 全銘柄、2026-09-18残高まで。原則第2営業日に公表 |
| margin-interest全銘柄日次 | 全銘柄、2026-09-25残高から。翌営業日公表 |
| margin-alert | 日々公表銘柄等の選別されたsubset。全銘柄残高の代替ではない |

margin-alertのPubDateは公表日、AppDateは残高基準の申込日で、規制適用開始日ではない。全銘柄日次のDateは残高日。後日訂正は公表・受領の文脈を保ち、公表前の当日残高を要求しない。subsetにないtickerを残高0にせず、全銘柄とunionして穴埋めしない。

PubDateは正確なAPI利用可能時刻の証明ではない。現行servingは公表日より後のas-ofから使い、公表日当日は含めない。公表日前の空応答、取得失敗、取得済み対象なしは別である。株数と金額はそれぞれ提供元の単位で保持し、欠測金額を株数×価格で補わない。指標の26週比較と株式basisは[市場環境](market-context.md#margin)に従う。

## 経済原本

取得時刻は受領・改訂の順序で、公表vintageや当時の既知値とは限らない。正常bulk応答の空日と取得失敗を分ける。最新原本が空・欠測でも古い非null値へ戻らず、取得失敗では既存原本を消さない。取得成功とL2計算可能は別の事実である。L2定義の退役・改名だけでsource factを消さない。

| source | 読む時に残す制約 |
| --- | --- |
| FRED・H.15 | 配信元ごとのrolling範囲・開始日がある。現在のCSV先頭は全歴史の保証ではなく、欠測セルと未使用列を残す |
| CFTC・ECB | long/shortや通貨の両脚を同じ原本から読む。古いnet/crossだけでは元値を復元できない |
| e-Stat | 頻度・季調・地域を固定。確報選択と速報を分け、確報欠測を速報へ戻さない。基準年は単位codeだけでは確定しない |
| BOJ・財務省 | 期間代表日と実公表日を分け、当月訂正・欠測も読む。月初/四半期代表日は実観測時刻ではない |
| 投資部門別 | 集計期間末とPubDateを区別。公表日cutoff以下の最新版を選び、取得時刻を当時の配信時刻としない |
| multpl | 日付付き月次表の表示日と推定注記を読む。現在の表で過去の日々のcurrent値は復元できない |
| PMI | 公式release PDFと月・URLを保持。headlineとflash/composite/sub-indexを混ぜず、L2抽出の失敗で原本受領を取り消さない |
| options | chain原本を保存し、snapshot/満期選択はL2。SQ直後のIV/skew欠測は構造的に生じ得る。緊急証拠金日は指定された清算値算出時の部だけを読む |

松井証券の買建評価損益率は同社内の信用買建の一系列で、全市場・追証人数・二市場信用残を直接観測しない。公表符号と注記を保ち、紹介算式から符号を修正しない。年のない表示日は受領JST日から14日以内の月日・曜日の一意候補による推定で、当時の公表年の証明ではない。再掲で鮮度を進めず、受領vintageを公表時刻と呼ばない。背景情報に限り、既存価格下落と独立した悪材料として加算しない。

## EDINET

一覧応答とCSV/XBRL ZIPの受領がL1、文書選択・抽出したmetrics/segment/debtがL2。Company Valuationのtype=5 CSV経路とResearch factsのtype=1 XBRL経路は別で、原本がなければ抽出履歴から捏造しない。明示したSQLite原本storeを再利用し、端末ZIPを暗黙に取り込まない。

<a id="edinet-metric-revision"></a>
### 抽出意味revision

EDINET_METRIC_REVISIONは数値・文書選択・訂正/取下げ・期間/連結/単位・null/0・quality/failure等の意味変更時に明示更新する。配置・import・transport retryでは更新しない。保存済み値・日時・旧release/hashを付け替えず、反映は[運用](../../jobs/OPERATIONS.md#edinet-metrics-revision)へ進む。releaseのproducer commitは各行の元抽出commitとは別である。

<a id="edinet-research-facts"></a>
### Company Research facts

セグメントと負債満期は直近年次・訂正とその比較年度から始まる任意datasetで、全提出履歴や全債務のcoverageではない。外部/内部売上、利益のbasis、連結範囲、当期/比較期、instant/durationと事業区分変更を揃える。企業独自conceptを名称の類似だけで標準指標へ割り当てない。

負債はJP GAAP明細表の明示元本。社債括弧内額を年別償還へ重ねず、リース・金利・担保・covenant・IFRS注記・5年超残差を完全に含む表とはしない。「－」・nil・未開示・未対応・未取得は0ではない。抽出okで0行もあり、取得失敗で以前のfactを消さない。

disclosed_onは提出JST日で、比較年度末へ遡及して利用可能にはならない。訂正は別docIDで保持し、同一releaseでも古い年次へfallbackしない。metricsと結ぶ時はtickerだけでなくas-of・原典期間・連結・通貨を揃える。借換え・将来FCF・維持投資はResearchの見積りであり、抽出factへ混ぜない。選択と照会は[固定release操作](../../jobs/OPERATIONS.md#edinet-research-query)に従う。

<a id="external-assessment"></a>
## 外部評価

媒体が提示した値、元評価者、尺度・対象期・basisと時点を読む。モデル価格、アナリスト予測/rating、Quants scoreは異なる結果。同じIFIS等の転載を独立票へ数えず、元評価者不明の独立性を推測しない。

latest/previousの差は同じorigin・単位・対象期・株式basis/尺度が揃う場合だけ計算する。初回、欠測回復、人数変更、同値の再受領は予想改善と別である。受領間のcomparableは自己評価への比較可能性を保証しない。最新の正常nullを古い非nullで埋めず、未取得・正常欠測・metadata未確認・障害を区別する。後日の取得失敗で既存観測を消さず、最新保存値を現在値や最新試行成功と呼ばない。

<a id="external-assessment-incremental-use"></a>
### 判断への利用

相違が利益・倍率・資本・期間の何を変えるかを先に問い、重要なものだけ原典で比較する。比較不能な外部値を使わずに調査を完成でき、銀行のEBITDA/FCF等を一律の不足要件にしない。ratingで順位加点せず、coverageのある企業だけの新laneを作らない。固定8社の比較では同basisの専門予想の増分価値と維持費を確認できず、原典の参照用途を採った。追加自動化は具体的な判断寄与と手作業の費用から選ぶ。[当時の採否](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-22-decision-quality-closure/report.md)は全将来効果の否定ではない。

Investing.comは個別の評価変更日・変更主体・新旧目標を調べる補助参照先で、日次の第二consensusには採用しない。同じ予想の転載を増やすより差分の判断寄与を優先した[選定記録](https://github.com/koumatsumoto/baibai-loop/blob/27daa52c4b63a8a2fc380e7f4f35b2e211be8e79/reports/studies/2026-10-22-public-external-sources/report.md)を残す。

### 公開HTMLの意味

マネックスはモデル価格と目標consensus、楽天は公開IFIS iframeのrating、株マップはQuants Researchの定量score、みんかぶはアナリスト欄のrating・目標・会社/外部予想を読む。投稿・総合目標・機械診断をアナリスト予想へ混ぜず、rating人数を利益予想の寄与者数へ流用しない。表示更新日・受領JST日・UTC時刻を分ける。過去比較列を過去日に受領した履歴へ変換しない。

公開HTML bodyとencodingはL1で、wire bytesや取得全体成功の証明ではない。楽天outer/iframeは同じ試行へ束縛する。共通の「予測情報なし」ページは無効codeにも返り、銘柄同一性未確認を正常nullへ変えない。みんかぶの404だけも未カバーの証明ではなく、同銘柄の明示表示が必要。403・429等を別経路で迂回しない。

みんかぶの対象期と単位は明示表記だけを使い、年だけから期末日、年度表からEPSや更新日を生成しない。表間の対象期衝突は観測全体を不採用として原本を残す。原評価元・株数basis・metric別人数が不明なら未知に保ち、自動deltaや一致票を作らない。

原本handoffは短期bundleであり、lakeへ公開して読戻すまで長期L1ではない。未保存の過去本文は復元できない。liveとreplayのparser選択は共通化しても、取得・receipt順序・失敗の意味は混ぜない。[取得・再開](../../jobs/OPERATIONS.md#external-assessment-acquisition)を参照する。

<a id="tradingview"></a>
### TradingView

L1はMCP SDK CallToolResultの保存可能な表現で、HTTP wire bytesやSDKが捨てたfieldは含まない。受領直後に原本を保存してからL2へ正規化する。snapshot_dateは受領JST日、master_asofは対象集合を選ぶ日で、**どちらもprovider公表日ではない**。fetched_at_utcで確かめられるのは実受領で、最初の公表や初回既知時刻は保証しない。

各行を実受領cutoff以内へ限定し、観測後の次の取引機会より前へ遡及させない。休日の同値を独立sampleにしない。正常rowのnull、明示unresolved、行がない未取得と技術障害を分ける。部分公開は全市場の代表標本ではなく、L1 replayでも元の受領時刻を維持する。

quote通貨とestimate通貨・単位は別。絶対対象期やmetricごとの期対応が未確認なら推定せず、FY/next FYやrating人数を一括流用しない。元metadataがあっても全metricの対応を保証しない。参考closeを株価の第二正本にしない。取得・復旧は[TradingView運用](../../jobs/OPERATIONS.md#tradingview-expectations)へ進む。

<a id="public-research-reports"></a>
## 公開企業レポート

[証券リサーチセンター](https://holistic-r.org/report/)と[フィスコ](https://www.fisco.co.jp/service/report/)は、選択済み企業の予想理由・事業仮説を調べる追加参照先。毎回2社を揃えたり、未カバー企業を除外したりせず、日次crawlerを追加しない。

SRCの独自予想と会社計画を凡例・対象期から区別する。フィスコの有償企業調査やissuer提供値は独立した賛成票としない。銘柄同一性、発行日、訂正・終了と実読ページを確認し、一覧/snippetだけを本文実読と記録しない。採用したHTTPS原本を既存sourceへ結び、事実・外部予測・作者仮定を分ける。原本に届かなければ一次資料へ戻る。

## Portfolioの価格とbenchmark

保有quoteはmarket storeのJ-Quantsを使い、欠測を公開quoteの台帳転記で補わない。補助quoteは時点とprice basisを示す。年次・3年・5年Outcomeのprimary benchmarkはJPX TOPIX gross total returnで、同期間・公表時点・gross区分を確認する。ETF価格を代用せず、[Outcome](portfolio-ledger.md#historical-outcome)の税費用basisと区別する。
