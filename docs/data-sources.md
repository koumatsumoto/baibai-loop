# データソース一覧とスコアリング

Baibai-Loop で使うデータソースを、客観性を優先した基準で選定して記録する。ニュース媒体の意見に偏らないよう **一次統計（中央銀行・政府・国際機関）中心** で構成し、一次統計で拾えない地政学イベントのみを補助ソースで補完する。

4 成分アーキテクチャ ([`architecture-v1.md`](./architecture-v1.md)) における各成分のデータソース対応:

| 成分 | 用途 | 主なソース |
| --- | --- | --- |
| `records/01-brief/` (a) | マクロ事実記録 | Tier 1（日銀・FRB・BLS・BOJ・JPX・FRED 等）+ Tier 2（Reuters / NHK / AP、地政学のみ） |
| `records/03-candidates/` (b) | 銘柄ふるい・valuation 指標 | J-Quants（銘柄一覧・日足・財務サマリー・決算予定日・営業日カレンダ）+ EDINET（財務諸表補完）+ JPX（特別注意 / 整理 / 取引停止 / 上場廃止警告の除外判定） |
| `records/02-outlook/` (c) | マクロ見解の組み立て | `records/01-brief/` の積み上げ（外部 API 直接参照なし） |
| `records/04-research/` (d) | 個別銘柄深掘り | J-Quants + EDINET + TDnet（開示文）+ JPX（資本コスト対応開示一覧）+ 必要時 brief 参照 |
| `records/05-trades/` | 執行記録 | 証券会社からの約定情報（手動記録） |
| `records/06-reviews/` | 事後検証 | `records/05-trades/` + 対象銘柄の株価推移（J-Quants） |

本ファイルの以下の節は主に **Tier 1 / Tier 2 一次統計**（brief 用）のスコアリングを扱う。screening / research で使う J-Quants / EDINET / TDnet の詳細仕様は [`screening/valuation-metrics.md`](./screening/valuation-metrics.md) を参照。

## 取得データの保存方針

J-Quants / EDINET から取得したデータは、個人利用・非公開 repository での Baibai-Loop 運用に限り、ローカル cache または永続 cache として保存してよいことを確認済み（2026-05-02、運用者確認）。外部公開・第三者再配布は行わない。

保存済み cache は、screening 再生成、ledger tracking、monthly retro のための入力証跡として扱う。ただし J-Quants の調整後価格、銘柄マスター、JPX 規制情報などは完全な point-in-time snapshot ではないため、再現性ではなく traceability の補助として使う。

## スコアリング軸

各軸 10 点満点、合計 30 点で評価する。

- **客観性**: 一次統計か、編集方針に党派性がないか
- **更新頻度**: 日次・週次での新鮮さ
- **取得容易性**: Web で誰でも読める / API・CSV 提供があるか

## Tier 1: 一次統計（10 媒体）

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
- [経済産業省](https://www.meti.go.jp/) — 鉱工業生産指数 / 商業動態統計

### 主要中央銀行（FRB・日銀に準ずる）

- [European Central Bank (ECB)](https://www.ecb.europa.eu/) — 欧州・金融政策声明文
- [ECB euro reference rates (historical CSV ZIP)](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) — EUR ベースの主要通貨レート。USD/JPY や AUD/JPY は `JPY/EUR ÷ USD/EUR`、`JPY/EUR ÷ AUD/EUR` で機械的に算出可。日次公表 (CET 16:00)
- [Bank of England (BoE)](https://www.bankofengland.co.uk/) — 英国・金融政策声明文

### 取引所・指数公表元（一次統計に準ずる）

- [CBOE VIX History CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv) — VIX は CBOE が公表する指数。指数公表元として一次扱い

これらはいずれも Tier 1 の既存媒体（総務省統計局・日本銀行・FRB 等）の対応機関、もしくは指数の公表元にあたり、追加スコアリング無しに Tier 1 として引用できる。

## Tier 1 の取得失敗時の扱い

Tier 1 / Tier 1 準拠 ソースが作業環境からアクセスできない場合、数値の代替埋めは**行わない**。brief 側で `データ取得失敗` と明示する（詳細は [workflow.md](./workflow.md) の「データ取得失敗時の運用」節を参照）。

- Tier 1 で取れない数値を Tier 2 / 補助外で埋めてはならない（一次統計の客観性が失われる）
- 連続 2 回の brief 作成で同じソースが取得失敗した場合、代替一次ソース（同じ統計を別 URL で配信している一次統計ミラー・集約サイト）の Tier 1 準拠追加を検討する
- 検討の結果、恒常的に取れないと判断した指標は、テンプレート側から該当行を落とすか、空欄運用で確定させる

### 既知の取得経路と代替ルート（2026-05 時点）

本リポジトリの作業環境では `fred.stlouisfed.org` への直接 HTTP リクエストが HTTP/2 stream INTERNAL_ERROR で打ち切られる（curl の `--http1.1` を付けても同じ）。FRED 経由で取りに行く前に、以下の Tier 1 / Tier 1 準拠 経路を優先的に試す:

| 指標 | 第一経路 (Tier 1) | 第二経路 (Tier 1 準拠) | 第三経路 (恒常的失敗時のみ) |
|---|---|---|---|
| 米 10Y / 2Y 利回り | Federal Reserve H.15 Selected Interest Rates | Federal Reserve datadownload Output.aspx (CSV) | Web Archive snapshot of FRED DGS10 / DGS2 |
| VIX | CBOE VIX History CSV | — | Web Archive snapshot of FRED VIXCLS |
| Brent 原油 | EIA RBRTEd.htm | — | Web Archive snapshot of FRED DCOILBRENTEU |
| WTI 原油 | EIA RWTCd.htm | — | Web Archive snapshot of FRED DCOILWTICO |
| USD/JPY | Federal Reserve H.10 weekly historical | ECB euro reference rates から `JPY/EUR ÷ USD/EUR` で算出 | Web Archive snapshot of FRED DEXJPUS |
| EUR/JPY | ECB euro reference rates (JPY 列) | — | — |
| AUD/JPY | ECB euro reference rates から `JPY/EUR ÷ AUD/EUR` で算出 | — | — |
| 日経平均 | （Nikkei 公式 indexes.nikkei.co.jp は 403） | （JPX 日次 PDF: テキスト抽出ツール必要） | Web Archive snapshot of FRED NIKKEI225 |
| TOPIX / 東証プライム売買代金 | JPX 日次レポート（PDF）。 PDF テキスト抽出ツール（poppler-utils / pdftotext / Python pdfminer / pypdf 等）が必要 | — | — |
| FedWatch (利下げ確率) | CME FedWatch Tool（HTTP 403 で取得不可） | — | — |

**Web Archive の使い方**: `https://web.archive.org/web/{TIMESTAMP}/{元 URL}` で snapshot を直接取得できる。`TIMESTAMP` は `YYYYMMDD` 8 桁または `YYYYMMDDHHMMSS` 14 桁。最新値が欲しい場合は観測日寄りのタイムスタンプを指定し、それでも snapshot が古い場合は別シリーズで複数 timestamp を試す。Wayback の snapshot は元ソースのキャッシュであり、引用は元ソース URL（FRED 等）として扱い、Wayback URL を併記する。

**ECB を使う前提**: ECB FX レートは日次 (CET 16:00) であり、週次の H.10 (米 NY noon) と timing が異なる。両者の差は通常 ±0.5 円以内。短期スパンでは互換とみなしてよいが、brief 内で USD/JPY を H.10 と ECB で混在させない（同一 brief 内では基準時刻を揃える）。

**PDF 抽出の前提**: BOJ の総裁記者会見・展望レポート PDF と JPX 日次統計 PDF は CMap encoded Type0 font を使うため、`pdftotext` (poppler-utils) か Python の `pdfminer.six` / `pypdf` が必要。本作業環境にこれらが入っていない場合、TOPIX や BOJ 政策決定本文は数値・本文ともに `データ取得失敗` 扱いになる。

## Tier 2 補助ソースの運用と例外

Tier 2 の Reuters / AP News / NHK は、Web 取得ツール側の制約で直接アクセスできない場合がある。その場合の暫定運用:

1. 数値データ（価格・利回り・金利）は Tier 1 のみで完結させる（FRED で多くが賄える）
2. 地政学イベントなどの事実記述で Tier 2 に届かない場合、CNBC / NPR / CSIS など事実報道中心の媒体を `[補助外]` タグ付きで一時引用してよい
3. `[補助外]` 引用は暫定であり、次回の週次 brief 作成時に Tier 2 引用への置換を 1 回試みる
4. **置換試行の打ち切りルール**: 連続 2 回置換に失敗した場合、その引用は `[補助外]` のまま受容して確定させる（恒常的に暫定扱いのまま放置しない）
5. `[補助外]` 引用でも意見記事・論評は取らず、事実記述部分に限定する

## 運用上のルール

- 記事を引用する際は事実記述部分に限定し、論評・予測・見通し部分は取らない
- 有料媒体（Bloomberg / 日経電子版 / WSJ など）は現時点では使わない
- このスコアリングは運用のなかで見直す。軸・媒体の追加削除は別途レビューする
