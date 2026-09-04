---
title: "データソース"
summary: "データソースの選択、Tier、取得失敗、保存境界の正本。"
doc_type: reference
status: active
source_paths:
  - "../../stores/"
related_docs:
  - "../architecture.md"
  - "./macro.md"
---

# データソース

Baibai Loopは、数値と経済事実を中央銀行、政府、国際機関などの一次情報源から取得する。Tier 2は、一次情報源で拾えない地政学事象と速報事実だけを補う。意見記事や論説は判断根拠にしない。

## 選択規則

| 必要な情報 | 選ぶ情報源 | 取得できない場合 |
| --- | --- | --- |
| 数値・経済統計 | Tier 1またはTier 1準拠 | 代替値を埋めず、取得失敗を記録する。後述のTier 2数値例外は全適用条件を満たす場合だけ使える |
| 地政学事象・速報事実 | Tier 2 | 事実報道中心の補助外情報源を明示して暫定利用できる |
| screening / researchの企業・市場事実 | 各領域の取得契約 | 本書でprovider仕様を重複定義せず、成果物別の正本へ従う |

情報源を取得できない場合は、別の情報源を探す前に、取得手段による遮断か配信元による遮断かを切り分ける。手順と既知の経路は[「Tier 1の取得失敗」](#tier-1-の取得失敗時の扱い)が所有する。

## 成果物別の情報源

各成果物の情報源を示す。層とstoreのauthorityは、[`architecture.md`の5層モデル](../architecture.md#1-5-層モデルと-4-役)と[Store authority](../architecture.md#store-authority)を正本とする。

| 成果物 | 用途 | 主な情報源 |
| --- | --- | --- |
| application DB `macro_context` | 必要時に重要な変化を調べる文脈 | Reuters等の記事 + Tier 1 / Tier 1準拠統計 + 必要な市場データ |
| screening run store | 銘柄ふるい・valuation 指標 | J-Quants（銘柄一覧・日足・財務サマリー・営業日カレンダ）+ EDINET（財務諸表補完、大量保有・公開買付の提出索引と買付価格）+ JPX（決算発表予定日、特別注意 / 整理 / 取引停止 / 上場廃止警告の除外判定、資本コスト対応開示一覧、上場廃止銘柄一覧） |
| application DB `thesis / thesis_review` | 個別銘柄深掘り | J-Quants + EDINET + TDnet（開示文）+ JPX（資本コスト対応開示一覧）+ 個別期待値へ影響するときだけmacro context参照 |
| application DB ledger | 執行記録 | 証券会社からの約定情報（人間報告だけを記録） |

本書は情報源Tier、取得失敗、保存境界を所有する。screening / researchのJ-Quants、EDINET、TDnet仕様は[`valuation-metrics.md`](./valuation-metrics.md)、macro providerの取得・vintage・retraction契約は[`macro.md`](./macro.md)を正本とする。

## 保有見直しの価格 fallback

Position Review・見積り calibration の価格 source は J-Quants(`stores/market/market.sqlite`)を primary とする。J-Quants が subscription / availability 問題で使えない場合だけ、公開 quote の daily close を手動 fallback として使い、ledgerまたはthesisのsource refへURL・取得日時・評価日・price basis・benchmark と同一 basis かを残す。basis が揃わない場合や corporate action の調整が確認できない場合は、確定評価ではなく provisional / inconclusive として扱う。

## Portfolio outcome benchmark

portfolio全体の年次・3年・5年outcomeは、JPXが公表する**TOPIX gross total return**だけをprimary benchmarkにする。operatorは公式factsheetまたは配当込み期間投資収益率の一次資料から、期間両端・公表日・as-of・gross区分・累積returnを入力draftへ正規化する。runtimeはHTTP/PDFを取得せず、期間や値を推測しない。published outcomeはbenchmark observationをpayloadに含めてapplication DBへ保存する。

`1321`や`1306`などのETF price proxyはportfolio outcomeの比較値として使用しない。J-Quants price-only proxyはscreeningまたは短期calibrationのdiagnosticに限り、総合収益率・年次benchmarkと表示しない。

## 取得データの保存方針

J-Quants / EDINETから取得したデータは、個人利用・非公開repositoryでのBaibai Loop運用に限り、local cacheまたは永続storeへ保存できる。外部公開・第三者再配布は禁止する。secret、token、認証headerはRaw metadata、manifest、logへ保存しない。

`method/`はscreening rules、macro reading rules、research playbookを所有する。`web/config/`はpresentation configurationを所有する。

大規模な market fact は4つの責務へ分ける。

| class | canonical form | rule |
| --- | --- | --- |
| L1 Raw | R2 immutable object | provider bytesを可能な限り原形で保持し、source request・retrieved-at・content hashを付ける |
| L1 Canonical | R2 Parquet + dataset / release manifest | field・型・日付・source identity・revision semanticsを正規化し、判断・score・rankを入れない |
| hydrated runtime copy | fixed L1 releaseから満たす`market.sqlite`のlake所有table | R2 authorityにしない |
| disposable byproduct | `.cache/` | canonical verification後に削除でき、入力証跡として扱わない |

Canonical manifestのsourceはtyped `SourceRef`で記録する。bytesを保持するprovider Rawは、実在するobject key、SHA-256、source側schema / manifest versionへ束縛する。provider、dataset、request range、metadata sidecarのkeyとSHA-256を固定し、ingest ID、object key、content digest、metadata versionを同時に照合する。

legacy SQLite snapshotはidentityだけを持ち、object keyを名乗らない。sealed copyはbuild中のstore変化を防ぐために作り、operation終了時に回収する。schema version、content digest、capture時刻によって、buildへ渡したstore世代を照合できる。logical manifestへR2 ETagを保存しない。

Raw retentionは、再取得が高価または不可能なPremium CSV、EDINET XBRL、JPX原本を`preserve`、routine API responseを`buffer`とする。`preserve`はGC候補にせず、budgetを設けない。`buffer`のsoft budgetは50 GiBであり、重要ingestを止めるhard capではない。current closureから未到達かつretrieved-atから90日以上の`buffer`だけを通常GCのplanへ載せる。削除直前にmetadata / object pairのidentityを再検証してlocal mirrorから削除し、R2の削除はBucket Lock満了後のDelete専用retention finalizerへ分離する。

screening L1のcanonical authorityはR2 releaseである。`stores/market/market.sqlite`のlake所有tableは、固定releaseから再構築するruntime copyであり、R2とdual canonical writeを行わない。取得範囲の帳簿とoperator導出factだけはSQLiteがcanonicalとなる。run storeは`stores/screening/runs.sqlite`を継続する。

releaseのrequired dataset、coverage、freshness、canonical objectのretention / GC、manifest version、authorityは[`market-lake.md`](./market-lake.md#market-lake-publication-contract)、screeningが読む入力の意味は[`screening-runtime.md`](./screening-runtime.md)を正本とする。table / columnの現行layoutはDB schemaが所有する。provider Rawのretentionは本節が所有する。

保存済み canonical fact は、screening 再生成・保有計測・見積り calibration のための入力証跡として扱う。J-Quants の調整後価格、銘柄マスター、JPX 規制情報などは完全な point-in-time snapshot ではないため、publication / effective / retrieved time と revision / coverage semantics が揃わない期間を完全再現可能とは扱わない。zero、complete snapshotでの無報告、coverage不足、parse failure、source unavailableを混同しない。

J-Quants の非公開レート制限と `bootstrap-cache` の per-asof 長期履歴 re-fetch コストは [`./screening-runtime.md`](./screening-runtime.md) §12 にまとめる。歴史週の生成が遅い / 完了しない場合はまずそこを参照する。

Macro indicatorは`baibai-engine macro`で公式API / CSVから取得する。data APIがない系列だけ、機械的なscraperを使える。AI agentのWebFetch出力を観測値として取り込んではならない。`stores/macro/macro.sqlite`はmacro contextの正本ではなく、期間検索、再取得抑制、判断材料確認のためのL1 storeである。

providerはsource identity、header / metadata、unit、finite、静的bandをstoreへの書き込み前に検証する。契約違反が1点でもあれば、そのseriesの取得結果を全件rollbackし、provider runを`failed`として残す。J-Quants / JPXの投資部門別売買状況は公表単位の千円を`jpy-thousand`として保持する。data APIがないPMIは、`spglobal_pmi` providerがgit管理のrelease URL manifestから公式PDFを取得し、headline値を公式定義域0〜100で検証する。refresh、vintage、retraction、registryの詳細は[`macro.md`](./macro.md)が所有する。

<a id="スコアリング軸"></a>

## 情報源Tierの基準

各軸 10 点満点、合計 30 点で評価する。

- **客観性**: 一次統計か、編集方針に党派性がないか
- **更新頻度**: 日次・週次での新鮮さ
- **取得容易性**: Web で誰でも読める / API・CSV 提供があるか

## Tier 1: 一次統計（12 媒体）

| # | ソース | 種別 | 主な対象 | 客観性 | 更新頻度 | 取得容易性 | 合計 |
|---|---|---|---|---|---|---|---|
| 1 | [日本銀行 統計](https://www.boj.or.jp/statistics/) | 中央銀行 | 日本・金融政策・金利・マネタリーベース | 10 | 8 | 8 | 26 |
| 2 | [内閣府 経済社会総合研究所](https://www.esri.cao.go.jp/) | 政府統計 | 日本・GDP / 景気動向指数 | 10 | 6 | 8 | 24 |
| 3 | [総務省 統計局](https://www.stat.go.jp/) | 政府統計 | 日本・CPI / 労働力調査 | 10 | 7 | 8 | 25 |
| 4 | [財務省](https://www.mof.go.jp/) | 政府統計 | 日本・貿易統計 / 国債 | 10 | 7 | 7 | 24 |
| 5 | [FRED (St. Louis Fed)](https://fred.stlouisfed.org/) | 集約データベース | 米国 / グローバル・主要経済指標 | 10 | 10 | 10 | 30 |
| 6 | [IMF World Economic Outlook](https://www.imf.org/en/Publications/WEO) | 国際機関 | グローバル経済予測 | 9 | 5 | 8 | 22 |
| 7 | [OECD Data](https://data.oecd.org/) | 国際機関 | 先進国マクロ指標 | 9 | 6 | 9 | 24 |
| 8 | [BIS Statistics](https://www.bis.org/statistics/) | 国際機関 | 国際金融・与信・為替 | 10 | 6 | 7 | 23 |
| 9 | [JPX マーケット統計](https://www.jpx.co.jp/markets/statistics-equities/) | 取引所 | 日本株市況・売買代金・信用残 | 10 | 10 | 9 | 29 |
| 10 | [CME FedWatch Tool](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) | 取引所 / 指標 | 米金利先物・利上げ期待 | 9 | 10 | 9 | 28 |
| 11 | [中国国家統計局 (NBS)](https://www.stats.gov.cn/) | 政府統計 | 中国・PMI / GDP / 不動産投資 / CPI | 9 | 7 | 6 | 22 |
| 12 | [中国海関総署](http://www.customs.gov.cn/) | 政府統計 | 中国・貿易統計 (輸出 / 輸入 / 国別) | 9 | 7 | 4 | 20 |

中国 NBS / 海関総署は政府統計として Tier 1 だが、Web 取得容易性は他 Tier 1 より低い (英語ページの個別 release URL の解決が困難な期間がある)。一次取得が継続困難な場合は §「一次統計の数値で Tier 1 取得が困難な場合の Tier 2 例外運用」を参照。

## Tier 2: 補助ソース（3 媒体）

Tier 1 で拾えない **地政学イベント** や **速報性が必要な事象** のみを対象とする。いずれも無料かつ事実記述中心。意見記事・論説は引用しない。

| # | ソース | 用途 | 客観性 | 更新頻度 | 取得容易性 | 合計 |
|---|---|---|---|---|---|---|
| 11 | [Reuters](https://jp.reuters.com/) | 地政学・速報（事実記述に限定） | 8 | 10 | 9 | 27 |
| 12 | [NHK ニュース](https://www3.nhk.or.jp/news/) | 日本語・比較的中立な国内報道 | 8 | 10 | 10 | 28 |
| 13 | [AP News](https://apnews.com/) | 通信社・事実ベース国際報道 | 9 | 10 | 9 | 28 |

## Tier 1 の準拠扱い

FRED は多くの一次統計の集約先として機能する。Tier 1 の適用を受ける政府機関・中央銀行の直接 URL も Tier 1 に準ずる扱いとする:

### 米国連邦機関

- [Bureau of Labor Statistics (BLS)](https://www.bls.gov/) — 米国・CPI / 労働統計
- [Bureau of Economic Analysis (BEA)](https://www.bea.gov/) — 米国・PCE / GDP / 国民経済計算
- [U.S. Census Bureau](https://www.census.gov/) — 米国・小売売上高 / 住宅着工
- [Federal Reserve Board](https://www.federalreserve.gov/) — 米国・金融政策声明文 / FOMC 議事要旨
- [Federal Reserve H.15 Selected Interest Rates](https://www.federalreserve.gov/releases/h15/) — 米国・Treasury constant maturity（10Y/2Y 等）。前後 5 営業日を保持。長期は [datadownload Output.aspx](https://www.federalreserve.gov/datadownload/Build.aspx?rel=H15) の CSV エクスポートで取得可
- [Federal Reserve H.10 Foreign Exchange Rates](https://www.federalreserve.gov/releases/h10/) — 米国・USD/JPY 等の noon-buying rate。週次公表 (月曜日)、`/Hist/dat00_ja.htm` などで歴史 CSV 取得可
- [U.S. Energy Information Administration (EIA)](https://www.eia.gov/) — 米国・原油 / 天然ガス・電力統計。Brent / WTI スポット価格は [RBRTEd.htm](https://www.eia.gov/dnav/pet/hist/RBRTEd.htm) / [RWTCd.htm](https://www.eia.gov/dnav/pet/hist/RWTCd.htm)。週次更新（火曜日 release date）

### 日本の省庁（総務省・財務省・日銀に準ずる）

- [厚生労働省](https://www.mhlw.go.jp/) — 有効求人倍率 / 毎月勤労統計
- [総務省統計局 統計ダッシュボード API](https://dashboard.e-stat.go.jp/static/api) — 各省の月次統計を安定した IndicatorCode で機械可読に配信する。認証不要。完全失業率（季節調整値）と名目賃金指数（現金給与総額）の取得経路。e-Stat の DB API が掲載を止めた統計（毎月勤労統計は 2021-10 で更新停止し、月次結果は release 毎のファイル資源のみ）を機械可読に読むために使う。数値は publisher の確報と一致することを release CSV で照合する
- [経済産業省](https://www.meti.go.jp/) — 鉱工業生産指数 / 商業動態統計
- [内閣府](https://www.cao.go.jp/) — 機械受注統計（民需・船舶電力を除く）/ 景気ウォッチャー調査（現状判断 DI）/ 景気動向指数。いずれも e-Stat の DB API で取得する。消費動向調査の消費者態度指数は DB 配信が無いため、同じ内閣府が組む景気動向指数の構成系列（先行 L6）として取り、そのぶん公表は調査本体より約 1 か月遅い
- [財務省 国債金利情報](https://www.mof.go.jp/jgbs/reference/interest_rate/index.htm) — 日本国債の主要年限別利回り。全履歴 CSV と当月 CSV を併用し、日次の 10 年国債利回りを取得する
- [日本銀行 コール市場関連統計](https://www.boj.or.jp/statistics/market/short/mutan/index.htm) — 無担保コール O/N 物レートの速報・確報。日次の確報 xlsx から平均レートを取得する

### 主要中央銀行（FRB・日銀に準ずる）

- [European Central Bank (ECB)](https://www.ecb.europa.eu/) — 欧州・金融政策声明文
- [ECB euro reference rates (historical CSV ZIP)](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) — EUR ベースの主要通貨レート。USD/JPY や AUD/JPY は `JPY/EUR ÷ USD/EUR`、`JPY/EUR ÷ AUD/EUR` で機械的に算出可。日次公表 (CET 16:00)
- [Bank of England (BoE)](https://www.bankofengland.co.uk/) — 英国・金融政策声明文

### 取引所・指数公表元（一次統計に準ずる）

- [CBOE VIX History CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv) — VIX は CBOE が公表する指数。指数公表元として一次扱い

これらはいずれも Tier 1 の既存媒体（総務省統計局・日本銀行・FRB 等）の対応機関、もしくは指数の公表元にあたり、追加スコアリング無しに Tier 1 として引用できる。

## Tier 1 の取得失敗時の扱い

Tier 1 / Tier 1 準拠 ソースが作業環境からアクセスできない場合、数値の代替埋めは**行わない**。macro context では取得できた source と取得できなかった source を分け、取得失敗した値を断定しない。

- Tier 1 で取れない数値を Tier 2 / 補助外で埋めてはならない（一次統計の客観性が失われる）
- 連続 2 回の macro context 作成で同じソースが取得失敗した場合、代替一次ソース（同じ統計を別 URL で配信している一次統計ミラー・集約サイト）の Tier 1 準拠追加を検討する
- 検討の結果、恒常的に取れないと判断した指標は、テンプレート側から該当行を落とすか、空欄運用で確定させる

### 取得失敗の切り分け：まず fetch tool を替える

取得失敗は、配信元の遮断ではなく取得手段だけが遮断されている場合がある。WebFetchが403を返しても、別の一次情報源を探す前に`curl`で1回確認する。

```bash
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
curl -sSL --max-time 25 -A "$UA" '<URL>' | head -c 2000
```

現在確認済みの経路を次に示す。応答が変わった場合は、同じ順序で取得手段と配信元を切り分け直す。

| host | WebFetch | `curl`（素） | `curl -A '<browser UA>'` | 通る経路 |
|---|---|---|---|---|
| `fred.stlouisfed.org` | 403 | 200 | 200 | `curl` で直接。`fredgraph.csv?id=<ID>` も通る |
| `www.meti.go.jp` | 403 | 403 | **200** | `curl` + browser UA が必須 |
| `www.jpx.co.jp` | 403 | 200 | 200 | `curl` で直接 |
| `www.tsr-net.co.jp` | — | 200 | 200 | HTML の `news/status/`、数値は `news/status/json/search.json`（`tsr_bankruptcies` provider と同じ endpoint） |
| `www.federalregister.gov` | API のみ 200 | 200 | 200 | WebFetch は HTML が 302 で `unblock.federalregister.gov` へ飛ぶ。`api/v1/documents.json?conditions[term]=<語>` なら WebFetch でも通る |
| `www.bls.gov` / `download.bls.gov` | 403 | 403 | 403 | **host 側の遮断。** 下記の代替を使う |

上表の経路でBLSの配信元へ届かない場合は、次のいずれかを使う。

- 数値: `https://api.bls.gov/publicAPI/v2/timeseries/data/<seriesID>`（無認証で直近 3 年。CPI 総合は `CUUR0000SA0`）
- 本文: Web Archive（下記の手順）

引用は元 source の URL として書き、経路（`curl` / API / Web Archive）を `used_for` に添える。

### 既知の取得経路と代替ルート

上の切り分けで直接取得できない指標は、以下の Tier 1 / Tier 1 準拠 経路を順に試す:

| 指標 | 第一経路 (Tier 1) | 第二経路 (Tier 1 準拠) | 第三経路 (恒常的失敗時のみ) |
|---|---|---|---|
| 米 10Y / 2Y 利回り | Federal Reserve H.15 Selected Interest Rates | Federal Reserve datadownload Output.aspx (CSV) | Web Archive snapshot of FRED DGS10 / DGS2 |
| VIX | CBOE VIX History CSV | — | Web Archive snapshot of FRED VIXCLS |
| Brent 原油 | EIA RBRTEd.htm | — | Web Archive snapshot of FRED DCOILBRENTEU |
| WTI 原油 | EIA RWTCd.htm | — | Web Archive snapshot of FRED DCOILWTICO |
| USD/JPY | Federal Reserve H.10 weekly historical | ECB euro reference rates から `JPY/EUR ÷ USD/EUR` で算出 | Web Archive snapshot of FRED DEXJPUS |
| EUR/JPY | ECB euro reference rates (JPY 列) | — | — |
| AUD/JPY | ECB euro reference rates から `JPY/EUR ÷ AUD/EUR` で算出 | — | — |
| 日経平均 | Web Archive snapshot of FRED NIKKEI225（`fred_csv` NIKKEI225） | — | — |
| 日経平均 PER / PBR | Nikkei 公式 indexes.nikkei.co.jp の `statistics/dataload` endpoint（`nikkei_indexes` provider。月次 HTML テーブルをブラウザ無しで取得） | — | — |
| TOPIX | J-Quants 専用 index bars endpoint（`jquants_indices` provider） | — | — |
| FedWatch (利下げ確率) | CME FedWatch Tool（HTTP 403 で取得不可） | — | — |

**Web Archive の使い方**: timestamp を推測せず、availability API で実在する snapshot を先に引く。8 桁 `YYYYMMDD` や年だけの短縮形は Wayback 側が最寄りへ redirect する経路で、HTTP 500 を返すことがある。

```bash
TS=$(curl -sS 'https://archive.org/wayback/available?url=<host/path>' \
     | python3 -c 'import json,sys;print(json.load(sys.stdin)["archived_snapshots"]["closest"]["timestamp"])')
curl -sS --compressed "https://web.archive.org/web/${TS}id_/<元 URL>"
```

`id_`は書き換えなしの原本を返すため、公表期を含む原題が残る。**`--compressed`は必須**であり、省略すると元のgzip byte列が返る。archive.orgは短時間の連続accessへHTTP 429を返すことがある。JSONでなくHTMLが返った場合は、間を置いて1回だけ再試行する。`archived_snapshots`が空なら別の情報源へ移る。Wayback snapshotは元情報源のcacheであり、引用は元情報源のURLとして扱い、Wayback URLを併記する。

**ECB を使う前提**: ECB FX レートは日次 (CET 16:00) であり、週次の H.10 (米 NY noon) と timing が異なる。両者の差は通常 ±0.5 円以内。短期スパンでは互換とみなしてよいが、macro context 内で USD/JPY を H.10 と ECB で混在させない（同一 macro context 内では基準時刻を揃える）。

**PDF 抽出の前提**: BOJ の総裁記者会見・展望レポート PDF と JPX 日次統計 PDF は CMap encoded Type0 font を使うため、`pdftotext` (poppler-utils) か Python の `pdfminer.six` / `pypdf` が必要。本作業環境にこれらが入っていない場合、TOPIX や BOJ 政策決定本文は数値・本文ともに `データ取得失敗` 扱いになる。

### 一次 source の記事 URL（推測で当てると 404 になる）

macro contextの`inputs.articles`で繰り返し使う日本の公表機関には、索引pageから辿らないと解決できないURL規則がある。確認済みの規則を次に示す。

| 公表 | URL 規則 |
|---|---|
| 財務省 為替介入実績（月次） | `https://www.mof.go.jp/english/policy/international_policy/reference/feio/monthly/<YYYYMMDD>e.html`（`feint` ではなく **`feio`**） |
| 総務省 消費者物価指数（速報） | `https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html`（`index.html` ではなく **`index-z.html`**） |
| 内閣府 四半期別 GDP 速報 | `https://www.esri.cao.go.jp/jp/sna/data/data_list/sokuhou/files/<YYYY>/qe<NNN>/tables/<table>.csv`（統計表はメニュー経由でしか辿れず、パスの推測は当たらない） |
| 厚生労働省 毎月勤労統計（速報） | `https://www.mhlw.go.jp/toukei/itiran/roudou/monthly/r08/<YYMM>p/dl/pdf<YYMM>p.pdf` |

**文字コード**: `www.mhlw.go.jp` と `www.stat.go.jp` の HTML は **cp932**。utf-8 で読むと例外にならず文字化けを素通しするので、デコードを明示する。

**timeout**: `www.ecb.europa.eu` は 30 秒では落ちる。60 秒へ延ばすと通る。

**curl でも通らない host**: `indexes.nikkei.co.jp`（Cloudflare の JS challenge で 403。日経 PER / PBR は `nikkei_indexes` provider の series で代替し、記事としては引用しない）、`www.stats.gov.cn`（接続 timeout。数値の代替埋めはせず `status: failed` の input として残し、中国は §意図的な境界 の proxy basket で読む）。

## Tier 2 補助ソースの運用と例外

Tier 2 の Reuters / AP News / NHK は、Web 取得ツール側の制約で直接アクセスできない場合がある。その場合の暫定運用:

1. 数値データ（価格・利回り・金利）は Tier 1 のみで完結させる（FRED で多くが賄える）
2. 地政学イベントなどの事実記述で Tier 2 に届かない場合、CNBC / NPR / CSIS など事実報道中心の媒体を `[補助外]` タグ付きで一時引用してよい
3. `[補助外]` 引用は暫定であり、次回の週次 macro context 作成時に Tier 2 引用への置換を 1 回試みる
4. **置換試行の打ち切りルール**: 連続 2 回置換に失敗した場合、その引用は `[補助外]` のまま受容して確定させる（恒常的に暫定扱いのまま放置しない）
5. `[補助外]` 引用でも意見記事・論評は取らず、事実記述部分に限定する

## 一次統計の数値で Tier 1 取得が困難な場合の Tier 2 例外運用

原則として「Tier 1 で取れない数値を Tier 2 / 補助外で埋めてはならない」(一次統計の客観性が失われる) が、**広く流通している経済統計のうち本作業環境からは Tier 1 個別 release URL が継続的に解決できない指標**については、以下の限定的な例外運用を許容する。

### 適用条件 (すべて満たすこと)

1. 元 Tier 1 が政府統計 (本ファイルの Tier 1 表に登録済み、例: 中国 NBS / 海関総署) であること
2. 取れない原因が data-sources.md 既知の取得経路と代替ルートで列挙された一次統計サイト側の構造的問題 (英語 individual release ページの URL 解決困難等) であること
3. 二次集計 (Trading Economics / FRED の集約コピー / Reuters 数値表示等) と一次統計の数値が一致する報道・配信が複数あること

### 運用

- macro context の`inputs.articles`で Tier 1 URL は`status: failed`（取得不能）として残し、Tier 2二次集計 URLは別の`input_id`で`status: ok`として追加する。`used_for`にTier 2例外であることを明記する
- material deltaまたはsizing cautionの`source_ids`には、採用根拠となる`status: ok`のinput_idを含める（failed inputだけを根拠にできない）
- 数値の前後に「Tier 2 二次集計、Tier 1 で確認できない期間」と注記し、macro context で引用する場合も同等の注記をつける
- 連続 2 回 (= 2 つの macro context cycle) で Tier 1 が取れない指標は、本ファイルの Tier 1 表に「個別 release URL 解決困難の運用注記」を追加し、暫定状態を可視化する
- 一次統計の数値が二次集計と乖離している場合 (=単一二次集計のみの値) は本例外を適用せず、analysis layer で「報道ベースの参考値」として質的に扱う

### 現在の例外運用対象

| 指標 | Tier 1 | Tier 2 暫定 | 失敗理由 |
| --- | --- | --- | --- |
| 中国 月次輸出 (国別含む) | [中国海関総署](http://www.customs.gov.cn/) | [Trading Economics](https://tradingeconomics.com/china/exports-yoy) | 個別 release URL の英語ページ解決が本作業環境で不能 |

## 運用上のルール

- 記事を引用する際は事実記述部分に限定し、論評・予測・見通し部分は取らない
- 有料媒体（Bloomberg / 日経電子版 / WSJ など）は現時点では使わない
- このスコアリングは運用のなかで見直す。軸・媒体の追加削除は別途レビューする
