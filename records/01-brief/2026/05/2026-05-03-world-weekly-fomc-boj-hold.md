---
type: periodic
scope: world
ai-draft: true
published_at: "2026-05-03T22:40:00+09:00"
sources:
  - "https://www.federalreserve.gov/releases/h15/"
  - "https://www.federalreserve.gov/releases/h10/Hist/dat00_ja.htm"
  - "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
  - "https://www.eia.gov/dnav/pet/hist/RBRTEd.htm"
  - "https://www.eia.gov/dnav/pet/hist/RWTCd.htm"
  - "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
  - "https://web.archive.org/web/20260503/https://fred.stlouisfed.org/graph/fredgraph.csv?id=NIKKEI225"
  - "https://www.jpx.co.jp/markets/statistics-equities/daily/index.html"
  - "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
  - "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm"
  - "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
  - "https://www.boj.or.jp/mopo/mpmdeci/state_2026/index.htm"
  - "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
---

# Brief World Weekly: 2026-05-03 world-weekly (fomc-boj-hold)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`/docs/design-principles.md`](/docs/design-principles.md) の「事実と分析の分離」節を参照）

対象期間: 2026-04-27 〜 2026-05-03
観測日: 2026-05-03
市場データの基準日: 指標ごとに直近営業日が異なるため値の右側に明記。米金利・USD/JPY 系・ECB クロス・原油は 2026-04-30 終値（米市場直近）、VIX は 2026-05-01、Brent / WTI は 2026-04-27（EIA の最新公表値）、日経平均は 2026-05-01（日本市場直近、5/3 〜 5/5 は GW 休場）。
前週 brief: [../04/2026-04-26-world-weekly-jp-cpi-retail.md](../04/2026-04-26-world-weekly-jp-cpi-retail.md)
直近の月次 brief: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。

月次統計（CPI / 雇用統計 / 政策金利変更等）はこのファイルでは原則記録せず、該当月の `macro-monthly` brief または先行 `world-daily` を参照する。日本 2026-03 全国 CPI と米 2026-03 小売売上高は、先行 daily brief [../04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md](../04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を参照。

> **ソース取得状況**: 本 brief 作成時点（2026-05-03）で fred.stlouisfed.org への直接 HTTP リクエストは HTTP/2 stream INTERNAL_ERROR で打ち切られた。代わりに以下の Tier 1 / Tier 1 準拠ソースを使用: 米金利 = Federal Reserve H.15 release（4-30 値まで）、USD/JPY = ECB euro reference rates から JPY/USD を機械的に算出（ECB は 4-30 まで日次公表、Federal Reserve H.10 weekly は 4-27 公表分が最新で 4-24 まで）、EUR/JPY・AUD/JPY = ECB 同上、VIX = CBOE 指数公表 CSV（5-1 まで）、Brent / WTI = EIA Petroleum Spot Prices（4-27 のみ。次回更新 2026-05-06）、日経平均 = Web Archive snapshot of FRED NIKKEI225（5-1 まで）。CME FedWatch は HTTP 403、JPX 日次 PDF は取得可だが本作業環境に PDF テキスト抽出ツール無く TOPIX・売買代金は `データ取得失敗`。

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 米10Y利回り | 4.40% | 前週 brief 表示値 4.31% から +9bp | [Federal Reserve H.15 Selected Interest Rates](https://www.federalreserve.gov/releases/h15/) (2026-04-30 値, 2026-05-03取得) |
| 米2Y利回り | 3.88% | 前週 brief 表示値 3.78% から +10bp | [Federal Reserve H.15 Selected Interest Rates](https://www.federalreserve.gov/releases/h15/) (2026-04-30 値, 2026-05-03取得) |
| 10Y-2Yスプレッド | +52bp | 前週 brief 表示値 +53bp から -1bp | 上記2値より算出 |
| VIX | 16.99 | 前週 brief 表示値 18.71 から -1.72pt | [CBOE VIX History CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv) (2026-05-01 終値, 2026-05-03取得) |
| Brent原油 | $113.89 | 2026-04-27 値。前週 brief 表示値 $111.86 から +1.8% | [EIA Europe Brent Spot Price FOB](https://www.eia.gov/dnav/pet/hist/RBRTEd.htm) (2026-04-27 値, EIA 当該系列は次回更新 2026-05-06, 2026-05-03取得) |
| WTI原油 | $99.89 | 2026-04-27 値。前週 brief 表示値 $98.42 から +1.5% | [EIA Cushing OK WTI Spot Price FOB](https://www.eia.gov/dnav/pet/hist/RWTCd.htm) (2026-04-27 値, EIA 当該系列は次回更新 2026-05-06, 2026-05-03取得) |
| FedWatch (次回会合 据置確率) | データ取得失敗 | CME ページが HTTP 403 を返却 | [CME FedWatch Tool](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) (アクセス不能, 2026-05-03取得試行) |

### 1.2 地政学・グローバルイベント

- 2026-04-29: FOMC は政策金利目標レンジを 3-1/2 〜 3-3/4 percent に維持すると公表。声明文では「the Committee will carefully assess incoming data, the evolving outlook, and the balance of risks」と記載。賛成 8、反対 4（Stephen I. Miran が 25bp 利下げを支持、Beth M. Hammack / Neel Kashkari / Lorie K. Logan が据置に同意するが声明文への easing bias 文言含めに反対）。声明文では「Inflation is elevated, in part reflecting the recent increase in global energy prices」とも記述。[Federal Reserve FOMC Statement 2026-04-29](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm) (2026-05-03取得)
- 2026-04-29: FOMC は声明文・Implementation Note・記者会見ページを公表。議事要旨は会合 3 週間後（5月下旬）公表予定。[Federal Reserve FOMC Calendars](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| USD/JPY | 156.56 | 前週 brief 表示値 159.35 から -1.75% | ECB の JPY/EUR (183.21) と USD/EUR (1.1702) から算出 [ECB euro reference rates](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) (2026-04-30 値, 2026-05-03取得。Federal Reserve H.10 weekly は 2026-04-27 公表分で 4-24 まで) |
| EUR/JPY | 183.21 | 前週 brief 表示値 186.71 から -1.87% | [ECB euro reference rates (historical CSV)](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) (2026-04-30 値, 2026-05-03取得) |
| AUD/JPY | 111.91 | 前週 brief 表示値 113.90 から -1.75% | ECB の JPY/EUR (183.21) と AUD/EUR (1.6371) から算出 [ECB euro reference rates](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) (2026-04-30 値, 2026-05-03取得) |

### 2.2 日本のイベント・速報

- 2026-04-27 〜 28: 日銀 金融政策決定会合が開催。「当面の金融政策運営について」（金融市場調節方針に関する公表文）と展望レポート（基本的見解）が 2026-04-28 12:04 JST に公表、展望レポート全文は 2026-04-30 14:00 JST 公表。[日本銀行 金融市場調節方針に関する公表文 2026年](https://www.boj.or.jp/mopo/mpmdeci/state_2026/index.htm) (2026-05-03取得)
- 2026-04-28: 日銀 総裁定例記者会見開催（PDF 掲載日 2026-04-30）。[日本銀行 総裁記者会見](https://www.boj.or.jp/about/press/index.htm) (2026-05-03取得)
- 2026-05-12: 日銀 2026-04-27 〜 28 会合の「主な意見」公表予定（8:50 JST）。[日本銀行 金融政策決定会合の運営](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)
- 2026-06-19: 日銀 2026-04-27 〜 28 会合の議事要旨公表予定（8:50 JST）。[日本銀行 金融政策決定会合の運営](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 日経平均 | 59,513.12 | 前週 brief 表示値 59,716.18 から -0.3% | Web Archive snapshot of [FRED NIKKEI225](https://web.archive.org/web/20260503/https://fred.stlouisfed.org/graph/fredgraph.csv?id=NIKKEI225) (2026-05-01 終値, 2026-05-03取得。fred.stlouisfed.org 直接アクセスは HTTP/2 stream INTERNAL_ERROR で失敗) |
| TOPIX | データ取得失敗 | JPX 日次 PDF は取得可だが本作業環境に PDF テキスト抽出ツール無し | [JPX マーケット統計](https://www.jpx.co.jp/markets/statistics-equities/daily/index.html) (取得済 PDF を解析不能, 2026-05-03取得試行) |
| 東証プライム売買代金 (日次平均) | データ取得失敗 | 同上 | [JPX マーケット統計](https://www.jpx.co.jp/markets/statistics-equities/daily/index.html) (取得済 PDF を解析不能, 2026-05-03取得試行) |

### 3.2 業種別騰落（週次、上位・下位各3）

データ取得失敗（JPX 週次集計を保持する PDF からの数値抽出を本作業環境で完了できず）。

### 3.3 日本株イベントカレンダー

- 2026-05-03 〜 05: 日本市場休場（憲法記念日・みどりの日・こどもの日）。[JPX 営業日カレンダー](https://www.jpx.co.jp/) (2026-05-03取得)

## 4. 差分データ（前週比）

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [`/docs/workflow.md`](/docs/workflow.md) の「差分データ」節を参照）。

### 4.1 閾値超えの変化

閾値は [`/docs/workflow.md`](/docs/workflow.md) の「差分データの閾値（週次）」節を参照。

- 🔸 Notable: USD/JPY: 159.35 → 156.56 (-1.75%)
- 🔸 Notable: EUR/JPY: 186.71 → 183.21 (-1.87%)
- 🔸 Notable: AUD/JPY: 113.90 → 111.91 (-1.75%)

判定対象外: TOPIX / 東証プライム売買代金 / FedWatch（取得失敗のため判定不能）。

### 4.2 方向履歴と方向反転

- 方向履歴: データ不足（観測対象週数: 3、`world-weekly` 系列が 3 週分）
- 方向反転: データ不足により判定不能

## 5. 次回主要イベント予定

- 2026-05-12: 日銀 2026-04-27 〜 28 会合の「主な意見」公表予定 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)
- 2026-05 下旬（会合 3 週間後）: FOMC 2026-04-28 〜 29 会合議事要旨公表予定 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)
- 2026-06-16 〜 17: FOMC 会合 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)
- 2026-06-19: 日銀 2026-04-27 〜 28 会合の議事要旨公表予定 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
