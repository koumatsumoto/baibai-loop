# データソース

数値は原公表機関か出所を確認できる公式集約先、企業調査は一次開示、broker事実は人間の確認報告を使う。Reuters・NHK・AP等の事実報道は速報・地政学の補助で、意見や数値の一次確認とは分ける。有料媒体は現行運用では使わない。sourceのtierは用途で、正確さ・利用許諾の保証ではない。

<a id="source-owners"></a>
## 取得範囲と実装の入口

取得・原本はData Collection、選択・単位変換・計算はL2、起動・再開は[運用](../../jobs/OPERATIONS.md)、保存は[architecture](../architecture.md#store-authority)が所有する。実装・取得・公開・本番切替を区別し、L2の有用性未確認を理由に許可済みL1の取得元・頻度・保持を縮小しない。

| 対象 | 正本 |
| --- | --- |
| 経済統計 | [catalog](../../engine/src/baibai_engine/data_collection/catalog.yaml)・[acquisition](../../engine/src/baibai_engine/data_collection/acquisition/)（L1全体の一覧ではない） |
| 株式・開示 | [J-Quants](../../engine/src/baibai_engine/data_collection/providers/jquants/)・[JPX](../../engine/src/baibai_engine/data_collection/providers/jpx/)・[EDINET](../../engine/src/baibai_engine/data_collection/providers/edinet/) |
| 記事 | [source契約](../../engine/src/baibai_engine/data_collection/providers/articles/contracts.py)・[投影/抽出](../../engine/src/baibai_engine/machine_analysis/observations/articles/) |
| 外部評価 | [公開HTML取得](../../jobs/src/baibai_jobs/acquisition/external_assessments/)・[TradingView原本](../../engine/src/baibai_engine/data_collection/providers/tradingview/) |

## 入手不能と利用条件

tool障害を原資料の不存在にせず、取得失敗で既存原本・観測を消さない。一次のAPI・CSV・索引を確認しても継続的なsource障害が残り、同期間・定義・値を複数経路で確認できる場合だけ、一次未確認と明示して二次配信を採る。単一値・不一致・一時障害では代用せず、AIが観測storeへ転記しない。失敗した入力と採用した根拠、Tier外の補助を明示する。

公表・更新・実受領・観測対象日を分け、未知の日付を受領日で埋めない。Web Archiveのcapture/closestは当時の公表・cutoff以前を保証せず、原URLとcaptureを残す。改訂前内容が不明なら未確認とする。契約範囲内の個人・非公開利用とし、原本全文・転載表を公開Issueへ再配布しない。

## J-QuantsとJPX

native HTTP pageがL1で、typed/flattened行から原本を作らない。空欄・未使用fieldも保持し、取得済み空・未取得・partialを分ける。普通株は完全なcodeと商品区分で識別し、末尾0・業種名では推定しない。calendarの元区分もbooleanから復元しない。[native操作](../../jobs/OPERATIONS.md#jquants-native-operations)で通常経路と切替を確認する。

DiscNoはidentityで訂正順ではない。開示日時・期間を用い、未保持vintageを完全PITとしない。比較座標は[企業財務](company-financials.md)・[企業成長](company-growth.md)。JPXは選択完了bundleだけを投影し、旧行を合流させず、消失を取消/不存在としない。受領完了は同時公表の証明ではない。現在の決算予定から過去予定を復元せず、会社IRで再確認する。

### 信用残のsourceと日付

全銘柄の週次・日次は残高対象日で使い分け、選別subsetのmargin-alertを全銘柄の欠損補完にしない。非掲載は0ではない。PubDateは公表日、AppDateは残高基準の申込日で規制開始日ではなく、全銘柄日次のDateも残高日。公表前の空・失敗・対象なしを分け、欠測金額を株数×価格で作らない。公表lag/cutoffは[reader](../../engine/src/baibai_engine/machine_analysis/observations/trading_activity/margin_reader.py)、比較/basisは[市場環境](market-context.md#margin)、取得・移行は[margin-alert](../../jobs/OPERATIONS.md#margin-alerts-native-l1)・[全銘柄残高](../../jobs/OPERATIONS.md#margin-interest-native-l1)・[空売り報告](../../jobs/OPERATIONS.md#short-sale-reports-native-l1)を参照する。

## 経済原本

受領履歴は公表vintage/当時の既知値とは限らない。最新原本の空・欠測を旧値で埋めず、取得失敗やL2定義の退役で原本を消さない。取得成功と計算可能も別。bulk応答・期間消失はacquisitionの規則に従い、暦日を人工補完しない。

| source | 解釈上の境界 |
| --- | --- |
| FRED・H.15 | 提供期間はrolling/開始日で限定され、CSV先頭は全歴史の保証ではない |
| CFTC・ECB | long/short・通貨両脚を保持。net/crossから原値を復元しない |
| e-Stat | 頻度・季調・地域を揃え、確報欠測を速報で埋めない。単位codeだけで基準年は決まらない |
| BOJ・財務省・投資部門別 | 期間代表日/集計末・公表・受領を分け、訂正と公表cutoffを読む |
| multpl | 月次表示と推定注記から過去の日々のcurrent値を復元したとしない |
| options | 満期選択はL2。SQ直後の欠測はあり得る。緊急証拠金日は指定部の清算値を使う |

松井の買建評価損益率は同社の信用買建に限る背景情報。全市場・追証人数を表さず、価格下落と独立した悪材料へ加算しない。符号を紹介算式で直さず、年推定を公表年の証明、再掲を鮮度更新としない。

<a id="pmi-public-articles"></a>
### PMIの公開記事経路（未有効化）

現行はS&P公式PDFの日本/米国製造業headline・サービスBusiness Activity Index。TE記事adapterは未接続で、同じ測定の別配信にすぎない。ISM・Output・Compositeへの代用、旧値/snippet補完、拒否後の自動fallbackはしない。原文受領と抽出成功、速報/確報・対象月・公表/配信時刻を分ける。完全PITを主張せず、取得・coverage・利用範囲の確認なしに切り替えない。[provider](../../engine/src/baibai_engine/data_collection/providers/tradingeconomics/)・[採否 #1477](https://github.com/koumatsumoto/baibai-loop/issues/1477)を参照する。

<a id="public-articles"></a>
## ニュース・機関公表記事

索引・本文受領・投影・主張抽出は別の進捗。見出し/概要/全文/部分の範囲と欠落を残し、失敗を出来事なしにしない。BBC World/Business・Fed・ECBの通常取得はRSS見出し・概要まで。BBC重複feedを独立source、Fed/ECB発言を政策決定としない。本文実読と判断接続の受入は[#1705](https://github.com/koumatsumoto/baibai-loop/issues/1705)。

案内・転送先・添付は別受領で出典の親子を保つ。財務省金利CSVの文字表現は経済系列と独立した測定ではなく、日銀XLSXのセル投影は図表・書式・計算結果の復元ではない。[exact回復](../../jobs/OPERATIONS.md#article-acquisition)は保存原本から行う。

## EDINET

一覧/ZIP受領がL1、文書選択・metrics/segment/debtがL2。type=5 CSVとtype=1 XBRLは別経路で、明示storeの原本を使う。抽出履歴から原本を作らず、端末ZIPを暗黙に取り込まない。同じdocID/typeの最初のbytesを保持し、通常refreshは再利用する。再抽出は再取得ではなく、提供元修復後の差替えは隔離取得・差分確認を伴う別操作。自動修復を保証しない。

<a id="edinet-metric-revision"></a>
### 抽出意味revision

EDINET_METRIC_REVISIONは抽出/選択の意味変更時だけ更新し、配置・import・retryでは変えない。旧値・時刻・release/hashを付け替えず、producer commitを各行の元抽出commitと同一視しない。[反映手順](../../jobs/OPERATIONS.md#edinet-metrics-revision)に従う。

<a id="edinet-research-facts"></a>
### Company Research facts

直近年次・訂正と比較年度の任意datasetで、全履歴ではない。内外売上・利益basis・連結・期間・区分変更を揃え、企業独自conceptを名前で推定しない。提出JST日以前へ遡及せず、最新訂正が不明なら旧年次で埋めない。[固定release選択](../../jobs/OPERATIONS.md#edinet-research-query)ではtickerだけでなく原典期間・連結・通貨を合わせる。

負債はJP GAAP表の明示元本で、社債括弧内額を重複計上しない。リース・利率・担保・covenant・IFRS注記・5年超残差を網羅しない。空/未対応/未取得は0でなく、抽出okでも0行があり得る。失敗で以前のfactを消さず、借換・将来FCF・維持投資の見積りを混ぜない。

<a id="external-assessment"></a>
## 外部評価

モデル価格・アナリスト予測/rating・Quants scoreを分け、転載を独立票にしない。origin・単位・対象期・株式basis/尺度を揃えた差だけを読み、初回・欠測回復・人数変更・再受領を改善としない。正常nullを旧値で埋めず、未取得・metadata不明・障害を区別する。保存最新値は現在値/最新試行成功の証明でなく、受領間比較が自己評価との比較を保証するものでもない。

<a id="external-assessment-incremental-use"></a>
### 判断への利用

利益・倍率・資本・期間の重要な相違だけ原典で確認し、比較不能値や銀行のEBITDA/FCF等を全社必須にしない。rating加点・coverage別laneは作らない。固定8社で増分価値/維持費が未確認だった[採否](https://github.com/koumatsumoto/baibai-loop/blob/88dcc68d768bbedf85df91fcfe7e7e30f76d0ed8/reports/studies/2026-10-22-decision-quality-closure/report.md)は、将来効果の否定ではない。Investing.comは評価変更の補助参照に限り、日次の第二consensusにしない。

### 公開HTMLの意味

媒体別の値・scope・metadataは[観測契約/normalization](../../engine/src/baibai_engine/machine_analysis/observations/external_assessments/)を正本とする。投稿/総合診断をアナリスト予想にせず、人数・年表示・過去比較列から未記載の予想人数・期末・EPS・受領履歴を作らない。銘柄同一性のない「情報なし」/404を正常欠測とせず、不明basis・期衝突からdelta/一致票を作らない。

body/encodingはL1でwire bytesではない。楽天outer/iframeは同じ試行に束縛し、403/429を迂回しない。短期handoffは公開・読戻しまで長期L1ではなく、未保存本文は復元不能。[取得・再開](../../jobs/OPERATIONS.md#external-assessment-acquisition)で原受領・失敗をreplayで書き換えない。

<a id="tradingview"></a>
### TradingView

L1は保存可能なSDK応答でwire bytesではなく、L2前に保持する。受領JST日・master選択日を公表日とせず、実受領cutoffと次の取引機会を守る。休日同値を独立sample、部分公開を市場代表標本としない。null・unresolved・未取得・障害を分け、replayでも元時刻を保つ。quote/estimateの通貨・単位・期対応をmetric別に確認し、FY/next FY・人数を一括流用しない。参考closeは株価の第二正本にしない。[受領・保存・履歴](../../jobs/OPERATIONS.md#tradingview-expectations)に従う。

<a id="public-research-reports"></a>
## 公開企業レポート

[SRC](https://holistic-r.org/report/)・[フィスコ](https://www.fisco.co.jp/service/report/)は選定企業の予想理由/事業仮説の追加参照。毎回取得・coverageを必須にせず、日次crawlerを作らない。独自予想と会社計画、sponsor調査と独立根拠を分け、銘柄・発行/訂正・対象期・実読ページを確認する。snippetを本文実読とせず、採用HTTPS原本を根拠へ結ぶ。届かなければ一次資料へ戻る。

## Portfolioの価格とbenchmark

保有quoteはJ-Quantsを使い、公開quoteの台帳転記で補わない。補助quoteの時点/basisを明示。年次・3年・5年Outcomeのprimary benchmarkはJPX TOPIX gross total returnで、同期間・公表時点・grossを確認しETF価格で代用しない。[Outcome](portfolio-ledger.md#historical-outcome)の税費用basisと区別する。
