---
title: "データソース"
summary: "情報源の選択、取得できない場合の扱い、利用・保存の境界。"
doc_type: reference
status: active
source_paths:
  - "../../stores/"
related_docs:
  - "../architecture.md"
  - "./market-context.md"
---

# データソース

## 選択規則

| 用途 | 採用する情報源 |
| --- | --- |
| 指標の数値・経済統計 | 原公表機関の一次情報、または出所を確認できる公式集約先 |
| 地政学・速報の事実 | Tier 2の事実報道。意見記事・論説は判断根拠にしない |
| 企業・市場の機械入力 | 現行providerとregistryの取得契約 |
| 企業の個別調査 | 会社の一次開示、EDINET、TDnet、JPX等の対象資料 |
| broker fact | 人間が確認して報告した注文・約定事実 |

Tierは用途の分類であり、正確さや利用許諾の点数ではない。原公表機関と集約先、公的予測と実績、市場の織り込みと観測値を区別する。

機械取得の仕様・column・単位変換はproviderとregistryを正本とする。指標の意味は[valuation metrics](./valuation-metrics.md)、指標観測・vintage・retractionは[Market Context](./market-context.md)に従う。

<a id="スコアリング軸"></a>

## 情報源の入口

### Tier 1・Tier 1準拠

| 対象 | 公式の入口 |
| --- | --- |
| 日本の金融政策・金融統計 | [日本銀行](https://www.boj.or.jp/statistics/) |
| 日本のGDP・景気 | [内閣府経済社会総合研究所](https://www.esri.cao.go.jp/)・[内閣府](https://www.cao.go.jp/) |
| 日本の物価・雇用・賃金 | [統計局](https://www.stat.go.jp/)・[厚生労働省](https://www.mhlw.go.jp/)・[統計ダッシュボードAPI](https://dashboard.e-stat.go.jp/static/api) |
| 日本の生産・商業 | [経済産業省](https://www.meti.go.jp/) |
| 日本の貿易・国債 | [財務省](https://www.mof.go.jp/)・[国債金利](https://www.mof.go.jp/jgbs/reference/interest_rate/index.htm) |
| 日本株の市場情報 | [JPX](https://www.jpx.co.jp/markets/statistics-equities/) |
| 米国統計 | [BLS](https://www.bls.gov/)・[BEA](https://www.bea.gov/)・[Census](https://www.census.gov/) |
| 米国金融政策・統計の集約 | [Federal Reserve](https://www.federalreserve.gov/)・[FRED](https://fred.stlouisfed.org/) |
| energy | [EIA](https://www.eia.gov/) |
| 国際比較・公的予測 | [IMF](https://www.imf.org/en/Publications/WEO)・[OECD](https://data.oecd.org/)・[BIS](https://www.bis.org/statistics/) |
| 欧州・英国の金融政策 | [ECB](https://www.ecb.europa.eu/)・[Bank of England](https://www.bankofengland.co.uk/) |
| 中国の政府統計 | [国家統計局](https://www.stats.gov.cn/)・[海関総署](http://www.customs.gov.cn/) |
| 市場の政策金利織り込み | [CME FedWatch](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) |
| 指数公表元 | [CBOE VIX History](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv)等、当該指数の公表元 |

### Tier 2

[Reuters](https://jp.reuters.com/)、[NHK](https://www3.nhk.or.jp/news/)、[AP News](https://apnews.com/)を事実報道の補助sourceとする。有料媒体（Bloomberg、日経電子版、WSJ等）は現行運用では使わない。

## Tier 1 の取得失敗時の扱い

取得toolの失敗と、原資料の不在を区別する。既存provider、同じ公表元のAPI・CSV・索引から確認し、失敗した一回のHTTP応答をhost全体の恒久的な状態として文書へ残さない。

| 情報 | 参照できる経路 |
| --- | --- |
| 米国金利 | [H.15](https://www.federalreserve.gov/releases/h15/)・[公式データ出力](https://www.federalreserve.gov/datadownload/Build.aspx?rel=H15) |
| 為替 | [H.10](https://www.federalreserve.gov/releases/h10/)・[ECB reference rates](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) |
| 原油 | [EIA Brent](https://www.eia.gov/dnav/pet/hist/RBRTEd.htm)・[EIA WTI](https://www.eia.gov/dnav/pet/hist/RWTCd.htm) |
| BLS系列 | 公式API `https://api.bls.gov/publicAPI/v2/timeseries/data/<seriesID>` |
| CPI公表資料 | [統計局の月次公表入口](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) |
| 機械取得済みの日本株指数・指標 | 対応するproviderと保存系列。記事本文を読んだ証拠にはしない |

これらは探索の入口であり、現在の到達性や取得可能期間の保証ではない。ECBとH.10のように観測時刻・定義が異なるsourceを、同じ系列として無説明に接続しない。

### Web Archive

過去資料のsnapshotはavailability APIで確認する。

```bash
curl --fail --get 'https://archive.org/wayback/available' \
  --data-urlencode 'url=<元資料のURL>' \
  --data-urlencode 'timestamp=<YYYYMMDDhhmmss>'
```

返却されたsnapshot URLとcapture時刻を使う。`closest`は分析cutoff以前を保証しない。cutoff後のcaptureでも元資料の公表がcutoff以前の場合はあるため、公表日と改定の有無で当時利用可能な内容を判断する。確認できない場合は未確認とし、元URL、公表時点、archive URL、capture時刻を分けて記録する。

## Tier 2 補助ソースの運用と例外

Tier 2に届かない地政学・速報の事実は、CNBC・NPR・CSIS等の事実報道を`[補助外]`と明示して利用できる。これは数値の代替許可ではない。引用の採否は今回の調査で決め、過去レポートの引用差し替えを定例作業にしない。

### 一次統計の数値を二次配信で補う条件

次の全条件を満たす場合だけ、一次未確認であることを明示して二次配信を使う。

1. 元sourceが上記の政府統計である。
2. 公式取得経路を確認しても、個別releaseの解決等の継続的なsource側の問題で取得できない。tool側の一時障害だけを理由にしない。
3. 同じ一次統計の同じ期間・定義・数値に一致する報道・配信を複数確認できる。

Market Context Assessmentでは、取得できなかったTier 1 URLを`status: failed`として残し、実際に読んだ二次sourceを別の`input_id`・`status: ok`で登録する。`used_for`と本文に「Tier 2二次配信・Tier 1未確認」を示し、判断の`source_ids`は実際に採用したok inputへ結ぶ。

単一の二次値しかない場合、または数値・期間・定義が一致しない場合は、この例外を適用しない。機械観測storeへAIが二次値を転記することも認めない。個別の取得障害や例外適用は当該調査へ記録し、回数を数えてactive文書へ追記する運用は行わない。

## 保有見直しの価格と欠測

保有提案の機械quoteはmarket storeのJ-Quantsから読む。欠損は未評価とし、公開quoteをledgerへ転記して補完しない。公開quoteをStock Assessmentの補助資料に使う場合は評価日・取得時点・price basisを示し、機械入力とは区別する。欠測の扱いは[保有提案](./investment-proposal.md#holding)を参照する。

## Portfolio outcome benchmark

portfolioの年次・3年・5年outcomeのprimary benchmarkはJPXのTOPIX gross total returnとする。公式factsheetまたは配当込み期間投資収益率から対象期間・公表時点・gross区分を確認し、1321・1306等のETF価格で代用しない。入力と評価・発行は[Portfolio Ledger](./portfolio-ledger.md#historical-outcome)に従う。

## 取得データの保存方針

取得データはsourceの契約・利用条件の範囲内で、個人利用・非公開運用に用いる。外部公開・第三者再配布は行わない。この文書は利用許諾を拡張しない。

保存場所と正本は[architecture](../architecture.md#store-authority)、L1の保持・公開・hydrateは[market lake](./market-lake.md)が所有する。正規化済みfactを保持していても、訂正前情報や当時の公開範囲を持たない期間まで完全なPITと扱わない。

保存済みEDINET文書一覧のL2だけをraw receiptから再構築する場合は、`baibai-engine screening rebuild-edinet-lists --start YYYY-MM-DD --end YYYY-MM-DD --sqlite-path PATH`を使う。指定範囲の全暦日にreceiptがあることを書込前に検査し、欠ける日は空応答とみなさず停止する。取得やarchive再処理は行わず、失敗した日を報告する。

EDINET archiveの再利用・取得後の保存は、明示したSQLite source storeだけを対象にする。以前の端末に残るローカルZIPは暗黙に取り込まない。

<a id="edinet-metric-revision"></a>

## EDINET metricの抽出意味revision

EDINET metricの`extractor_revision`は明示的な抽出意味revisionであり、source bytes/import pathから導出しない。数値・文書選択・訂正/取下げ・cache完全性・ticker/期間/連結/単位・NULL/0・quality/failure・保存時型変換の意味変更時に更新する。配置・import・transport retryだけでは更新しない。初版は意味同一を確認した旧tokenを継承し、保存済み行・値・日時・旧release/hashを変更しない。[意味変更時の反映](../../batch/OPERATIONS.md#edinet-metrics-revision)はbatch運用が所有する。releaseの`producer_git_commit`は公開処理のcommitであり、各行の元抽出commitとは区別する。

<a id="edinet-research-facts"></a>

## EDINETのResearch補助fact

`edinet.segment_facts`と`edinet.debt_schedule`は、有価証券報告書・訂正有価証券報告書のtype=1 XBRL ZIPから保存する任意のL1入力である。Researchの判断やscreening/calibrationの採否条件は変えない。初回収録は既存document inventoryの各issuerの直近年次とその訂正、そこに含まれる比較年度に限定する。その後は収録した書類を保持し、新規年次・訂正を追加する。取得開始前の全提出履歴は保証しない。

セグメントは標準数値elementだけを対象とし、外部売上と内部売上込みの売上、営業・経常・税引前・純利益・issuer定義利益のbasis、連結・非連結、当期・比較期を分ける。資産はinstant、売上・利益はdurationである。事業、その他、調整、合計を分け、QNameと原典の日本語member labelを残す。企業独自elementは名称の類似で標準指標へ割り当てない。事業区分の変更をまたいだ増減比較は原典を確認する。

負債はJP GAAPの借入金等明細表・社債明細表の明示された元本だけを対象とする。短期借入金、1年内返済予定長期借入金、長期借入金の年別返済、社債の年別償還を区分し、原典の円・千円・百万円をJPYへ換算する。社債残高表の括弧内額は年別償還表へ加算しない。リース、金利、担保、covenant、5年超の残差推計、IFRSの金融負債注記は対象外であり、全債務の完全な満期表ではない。未知の見出しや結合セルのある表は未対応として扱う。

値の0と欠測を区別する。表の「－」・nilはnullとし、未開示・未対応・未取得を0で補わない。抽出の成否と理由は既存`market.source_coverage`の`source=edinet_research_facts`、`coverage_key=docID`に保存する。`status=ok`の`error`欄は抽出revision・dataset別missing reasonsのJSON、`failed`は取得・抽出失敗理由である。okでも行数0になり得る。書類別の保存はatomicで、取得失敗時は以前のfactを消さない。

`disclosed_on`は提出日時のJST日付であり、比較年度の期末日に遡って利用可能にはならない。source提出日時はtimezone付きで保持し、source locatorはZIP内のfile・QName・context、負債では0始まりのtable/row/cell位置まで指す。訂正書類は別docIDとして保持する。同じ書類の情報修正や公開状態の履歴が確定できない場合は未確認とし、現在取得したbytesを過去時点の完全な再現とは扱わない。

固定releaseからの選択と欠測時の扱いは[Research query](./market-lake.md#edinet-research-query)、初期化・運用は[batch運用](../../batch/OPERATIONS.md#edinet-research-facts)に従う。

<!-- 保存された2026-10-12 studyの原引用先。現行名の主anchorはexternal-assessment。 -->
<a id="retail-dynamics"></a>
<a id="external-assessment"></a>

## 外部参考評価の観測と解釈

External Assessmentは媒体が提示した値・出所・尺度・時点を参照する。掲載先と原評価元を区別し、同じIFIS由来の目標値/ratingを独立した賛成票として数えない。原評価元が不明な系列の独立性は推測しない。外部モデル価格・アナリスト予測/rating・Quants scoreは別の型・結果であり、正規化した多数決点や独自FVへ統合しない。

同じ配信元の受領間を、確認できた同じorigin・単位・株式基準・対象期間または尺度で比較する。欠測の回復・人数変更・同値の再観測は数値改善の主張と区別する。既存の企業固有評価は[Stock Assessment](./stock-assessment.md)が所有する。型と比較規則は`external_assessment/models.py`・`analysis.py`、cutoffと選択・保存契約は[market lake](./market-lake.md#external-assessment-query)を参照する。

取得・観測の状態は運用上の利用可能性を示す。[ルールの目的区分](./screening-runtime.md#rule-purpose)にある投資上の資格・判断や、その経済的最適性の根拠にはしない。

<!-- 歴史reportからの引用anchorを保持する。runtimeの旧名aliasではない。 -->
<a id="external-assessment-acquisition"></a>

## 公開brokerページの観測

`external_assessment/acquisition`は既存のL1取得・保存adapter名であり、個人投資家の売買動向を表すdomain名ではない。配信画面で受信できた値を保存する。マネックスの公開Scouter Lite理論株価表から、目標株価コンセンサスとPER会社予想・PERアナリスト予想・PBRのモデル価格を取得する。目標株価の原データとモデル価格の算出主体は別々に記録する。ロックされたsummaryや割安・割高の目盛りから判断を作らない。

楽天の公開forecastページが指定するIFIS iframeから現在のレーティングを取得する。別の業績予想欄のアナリスト数をレーティング人数へ流用せず、確認できない尺度・方向・人数・公表日時はnullに保つ。画面の過去列は取得以前の受信履歴にはしない。両社が同じIFIS情報を配信しても独立した賛成票とは数えない。

株マップの銘柄診断からQuants Researchの定量scoreを別型で取得する。成長性・割安性・企業規模・テクニカル・財務健全性・市場トレンドは10点満点、総合点と業種平均は100点満点であり、人間のアナリストconsensusや目標株価へ換算しない。明示表示更新日を`score_as_of_date`として保存し、取得UTC時刻・JST受領日と区別する。factor欄のPER等がNAでも表示scoreが有効なら保持する。株マップの銘柄同一性・6軸の構造・尺度・表示更新日を確認できない応答は正常nullへ変えない。公式の[計算score説明](https://jp.kabumap.com/servlets/kabumap/Action?SRC=navigation/util/base&page=3)に従い、割安性はPER/PBR/75日移動平均乖離率等に基づく提示として扱う。英字ticker・銀行を媒体取得の対象から外さない。

公式outerのRICとiframe URLは要求先を確認する。観測の銘柄同一性はiframe応答の比較表または確認済みchart bindingで確かめる。「業績予測情報はありません」だけの共通ページは無効codeにも返るため、`no_estimates_identity_unconfirmed`として未収録に保つ。この理由は当該銘柄の予測・レーティングの不存在を確定したものではなく、正常nullの観測とも区別する。

楽天の保存観測が対象時点までにない場合も、External Assessmentは`distributor=rakuten`・`inputs.latest=null`・比較状態`not_observed` / 理由`no_observation`を返す。これは取得未試行・未確認shell・技術障害等のどれかを推測する結果ではなく、読取storeに観測がないという意味である。正常nullは元受領時刻を持つ観測行と`missing` / `latest_missing`で区別し、画面とResearch入力にも同じ結果を渡す。取得の具体的な失敗理由はprivate cohort stageで確認する。後日の取得失敗は既存観測を消さないため、表示値は受領時刻付きの最新保存観測であり、現在値や最新試行成功を保証しない。

受信時刻とJST受信日、媒体が明示した更新日を区別する。保存と直近受領の選び方は[market lake](./market-lake.md#external-assessment-query)、比較の意味は[外部参考評価](#external-assessment)に従う。現adapterでは`share_basis`・評価対象期間・レーティング尺度が未確認のnullであり、数値deltaは基本的に`not_comparable`となる。受領履歴は利用できるが数値の変化比較は未実用の範囲を残し、未確認metadataを推測で補完しない。分析は入力参照とmethod versionを持つ読取結果であり、投資判断へ自動投入しない。

抽出済みtyped観測とDB/L1履歴を正本とする。日次の固定pair取得では公開HTTP 200の元本文とencodingを約3日のprivateな診断用データとして保存する。raw保存だけの失敗はwarningで知らせ、正常typedの保存・公開を妨げない。HTTP成功の原応答と、銘柄同一性・値が確認できたtyped観測は別であり、parse失敗や未確認の共通ページを正常観測へ変えない。過去に保存しなかった本文は取得当時の原応答として復元できない。日付別保持・候補確認・読戻しは[batch運用](../../batch/OPERATIONS.md#日次取得と固定コホートの再開)に従う。

限定取得・保存済みstageの再投入は[batch運用](../../batch/OPERATIONS.md#external-assessment-acquisition)、保存と照会は[market lake](./market-lake.md#external-assessment-query)を参照する。

## TradingViewの市場期待

`tradingview.forecast_snapshots`はOfficial MCPの`get_symbol_data_batch`から実際に取得した市場期待を、日付・銘柄ごとに保持する任意のL1入力である。対象は既存J-Quants masterの東証普通株で、Screeningの最小株価履歴を要求しない。`get_forecasts`の日本株通貨補正や、ScreenerによるUniverse生成は行わない。

PITが保証するのは`fetched_at_utc`時点の取得値であり、providerの最初の公表時刻ではない。`snapshot_date`は実観測日のJST日付、master基準日は対象Universeを選ぶ日付であり、観測値の時点ではない。collectorは手動・定期ともJPX calendarで判定し、営業日は当日masterで15:30以降、休場日は直近営業日の保存済みmasterで09:00以降に取得する。[明示的な手動観測](../../batch/OPERATIONS.md#保存済みmasterを使う当日の手動観測)はcalendarが要求するmaster基準日との一致を確認し、当日の実受領時刻を保持する。営業日の引け前を許可しない。受領済み応答の[明示的な受入](../../batch/OPERATIONS.md#保存済み実観測の明示的な受入)も同じ観測日の意味で記録する。利用可能になる最速時点は観測後の次の取引機会とし、masterの日付や日付だけで過去の売買時点へ遡及させず、翌日値や無条件のlatest-nonnullによる欠測の埋め直しもしない。休日の同値履歴を独立した情報sampleとして扱わず、各行の`fetched_at_utc`を評価のas-of cutoff以下に限定する。

正常rowのnull、正常応答で明示された`unresolved`、rowがない未取得銘柄を区別する。正常なbatchごとに保存し、同日の後続取得はrowがない銘柄だけを対象にする。各銘柄の最初の正常観測と`fetched_at_utc`は同日中に上書きしない。429や応答不良を未取得銘柄の`unresolved`へ変換せず、部分集合を全市場の代表標本とみなさない。過去のsnapshotが最新releaseに残っていても、今日の取得成功とは扱わない。

quoteの通貨は同じresponseの`currency`を保持する。estimateの通貨・unit、quote/provider更新時刻、絶対対象期は未確認のためnullとし、JPYや特定の決算期を推測して入れない。`*_estimate_fy`と`*_forecast_next_fy`の対象期が同じとは限らず、`recommendation_total`はEPS・売上・目標株価それぞれの寄与者数ではない。参考closeは市場期待の取得時点を読む補助で、株価の第二正本にはしない。

取得・認証復旧は[batch運用](../../batch/OPERATIONS.md#tradingview-expectations)、固定releaseの照会は[market lake](./market-lake.md#tradingview-query)を参照する。Screeningの順位、FV、Research判断への自動混入は行わない。


### 銘柄マスターの原本と普通株判定

J-Quants `eq/master` の取得応答は`jquants.source_pages`にL1原本として保存する。
そこから採用した`jquants.master_snapshots`はL2で、取得基準日・完全な5文字の銘柄コードとrecord JSONを保持する。
商品区分、英文名、17/33業種コード、規模区分、貸借区分などを取得時に削らず、
副銘柄も保持する。L2の読取では末尾0の主銘柄を4文字tickerへ投影し、
[商品区分](https://jpx-jquants.com/ja/spec/eq-master/product-category) `ProdCat=011`
と組み合わせて国内普通株を識別する。末尾0だけではETF・REIT等も含むため十分でなく、
業種名から商品種類を推測しない。対象市場の選択はこの判定とは別に行う。
旧派生列だけのマスターから原本項目を復元しない。切替時は取得済み原本があれば再利用し、
原本のない基準日についてはマージ後に再取得する。
