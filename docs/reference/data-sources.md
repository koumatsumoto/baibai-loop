---
title: "データソース"
summary: "情報源の選択、取得できない場合の扱い、利用・保存の境界。"
doc_type: reference
status: active
source_paths:
  - "../../stores/"
related_docs:
  - "../architecture.md"
  - "./macro.md"
---

# データソース

## 選択規則

| 用途 | 採用する情報源 |
| --- | --- |
| Macroの数値・経済統計 | 原公表機関の一次情報、または出所を確認できる公式集約先 |
| 地政学・速報の事実 | Tier 2の事実報道。意見記事・論説は判断根拠にしない |
| 企業・市場の機械入力 | 現行providerとregistryの取得契約 |
| 企業の個別調査 | 会社の一次開示、EDINET、TDnet、JPX等の対象資料 |
| broker fact | 人間が確認して報告した注文・約定事実 |

Tierは用途の分類であり、正確さや利用許諾の点数ではない。原公表機関と集約先、公的予測と実績、市場の織り込みと観測値を区別する。

機械取得の仕様・column・単位変換はproviderとregistryを正本とする。指標の意味は[valuation metrics](./valuation-metrics.md)、macro観測・vintage・retractionは[macro](./macro.md)に従う。

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

Macro Contextでは、取得できなかったTier 1 URLを`status: failed`として残し、実際に読んだ二次sourceを別の`input_id`・`status: ok`で登録する。`used_for`と本文に「Tier 2二次配信・Tier 1未確認」を示し、判断の`source_ids`は実際に採用したok inputへ結ぶ。

単一の二次値しかない場合、または数値・期間・定義が一致しない場合は、この例外を適用しない。機械観測storeへAIが二次値を転記することも認めない。個別の取得障害や例外適用は当該調査へ記録し、回数を数えてactive文書へ追記する運用は行わない。

## 保有見直しの価格と欠測

Position Reviewの機械quoteはmarket storeのJ-Quantsから読む。欠損は未評価とし、公開quoteをledgerへ転記して補完しない。公開quoteをThesisの補助資料に使う場合は評価日・取得時点・price basisを示し、機械入力とは区別する。欠測の扱いは[Position Review](./position-review.md)を参照する。

## Portfolio outcome benchmark

portfolioの年次・3年・5年outcomeのprimary benchmarkはJPXのTOPIX gross total returnとする。公式factsheetまたは配当込み期間投資収益率から対象期間・公表時点・gross区分を確認し、1321・1306等のETF価格で代用しない。入力と評価・発行は[Portfolio Ledger](./portfolio-ledger.md#historical-outcome)に従う。

## 取得データの保存方針

取得データはsourceの契約・利用条件の範囲内で、個人利用・非公開運用に用いる。外部公開・第三者再配布は行わない。この文書は利用許諾を拡張しない。

保存場所と正本は[architecture](../architecture.md#store-authority)、L1の保持・公開・hydrateは[market lake](./market-lake.md)が所有する。正規化済みfactを保持していても、訂正前情報や当時の公開範囲を持たない期間まで完全なPITと扱わない。
