---
type: periodic
scope: world
ai-draft: true
published_at: "2026-05-03T22:30:00+09:00"
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
  - "https://www.census.gov/retail/sales.html"
  - "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html"
  - "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
  - "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
---

# Brief World Weekly: 2026-04-26 world-weekly (jp-cpi-retail)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`/docs/design-principles.md`](/docs/design-principles.md) の「事実と分析の分離」節を参照）

対象期間: 2026-04-20 〜 2026-04-26
観測日: 2026-05-03（backfill: 当該週内に作成できず、翌週末時点で遡及作成）
市場データの基準日: 2026-04-24 終値（直近営業日）
前週 brief: [2026-04-19-world-weekly-us-iran-deescalation.md](./2026-04-19-world-weekly-us-iran-deescalation.md)
直近の月次 brief: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。

2026-04-24 に先行 daily brief [2026-04-24-world-daily-jp-cpi-mar-us-retail.md](./2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を作成済み。月次級データ（米 2026-03 小売売上高、日本 2026-03 全国 CPI）はそちらを参照し、本 brief では再掲しない。

> **ソース取得状況**: 本 brief 作成時点（2026-05-03）で fred.stlouisfed.org への直接 HTTP リクエストは HTTP/2 stream INTERNAL_ERROR で打ち切られた。同等の一次値を以下の Tier 1 / Tier 1 準拠ソースから取得した: 米金利 = Federal Reserve H.15 release、USD/JPY = Federal Reserve H.10 weekly historical、EUR/JPY・AUD/JPY = ECB euro reference rates（USD/JPY と同様に central-bank 公表値、EUR ベースから機械的に算出）、VIX = CBOE 指数公表 CSV、Brent / WTI = EIA Petroleum Spot Prices、日経平均 = Web Archive（Wayback Machine）に保存された FRED NIKKEI225 シリーズの 2026-05-01 終値時点 snapshot。CME FedWatch は HTTP 403、JPX 日次 PDF は取得可だが本作業環境に PDF テキスト抽出ツール（poppler / pdftotext）が無く TOPIX・売買代金の数値抽出はできず `データ取得失敗` 表記とする。

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 米10Y利回り | 4.31% | 前週 brief 表示値 4.31% から 0bp | [Federal Reserve H.15 Selected Interest Rates](https://www.federalreserve.gov/releases/h15/) (2026-04-24 値, 2026-05-03取得) |
| 米2Y利回り | 3.78% | 前週 brief 表示値 3.81% から -3bp | [Federal Reserve H.15 Selected Interest Rates](https://www.federalreserve.gov/releases/h15/) (2026-04-24 値, 2026-05-03取得) |
| 10Y-2Yスプレッド | +53bp | 前週 brief 表示値 +50bp から +3bp | 上記2値より算出 |
| VIX | 18.71 | 前週 brief 表示値 17.48 から +1.23pt | [CBOE VIX History CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv) (2026-04-24 終値, 2026-05-03取得) |
| Brent原油 | $111.86 | 前週 brief 表示値 $98.05 から +14.1% | [EIA Europe Brent Spot Price FOB](https://www.eia.gov/dnav/pet/hist/RBRTEd.htm) (2026-04-24 値, 2026-05-03取得) |
| WTI原油 | $98.42 | 前週 brief 表示値 $93.40 から +5.4% | [EIA Cushing OK WTI Spot Price FOB](https://www.eia.gov/dnav/pet/hist/RWTCd.htm) (2026-04-24 値, 2026-05-03取得) |
| FedWatch (次回会合 据置確率) | データ取得失敗 | CME ページが HTTP 403 を返却 | [CME FedWatch Tool](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) (アクセス不能, 2026-05-03取得試行) |

### 1.2 地政学・グローバルイベント

- 2026-04-21: 米国 2026-03 小売売上高（Advance）が公表済み。月次級データのため本 brief では再掲せず先行 daily brief [2026-04-24-world-daily-jp-cpi-mar-us-retail.md](./2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を参照。[U.S. Census Bureau](https://www.census.gov/retail/sales.html) (2026-05-03取得)
- 2026-04-28 〜 29: FOMC 会合の開催日程が公表されており、当該週の翌週に開催予定として確認できる。[Federal Reserve FOMC Calendars](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| USD/JPY | 159.35 | 前週 brief 表示値 約159.5 から -0.1% | [Federal Reserve H.10 Historical Rates for the Japanese Yen](https://www.federalreserve.gov/releases/h10/Hist/dat00_ja.htm) (2026-04-24 noon-buying rate, 2026-05-03取得) |
| EUR/JPY | 186.71 | 前週 brief は取得失敗のため初回値（前週比なし） | [ECB euro reference rates (historical CSV)](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) (2026-04-24 値, 2026-05-03取得) |
| AUD/JPY | 113.90 | 前週 brief は取得失敗のため初回値（前週比なし） | ECB の JPY/EUR (186.71) と AUD/EUR (1.6393) から算出 [ECB euro reference rates](https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip) (2026-04-24 値, 2026-05-03取得) |

### 2.2 日本のイベント・速報

- 2026-04-24: 日本 2026-03 全国 CPI が公表済み。月次級データのため本 brief では再掲せず先行 daily brief [2026-04-24-world-daily-jp-cpi-mar-us-retail.md](./2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を参照。[総務省統計局](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) (2026-05-03取得)
- 2026-04-27 〜 28: 日銀 金融政策決定会合の開催日程が公表されており、当該週の翌週に開催予定として確認できる。[日本銀行 金融政策決定会合の運営](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 日経平均 | 59,716.18 | 前週 brief 表示値 58,476 から +2.1% | Web Archive snapshot of [FRED NIKKEI225](https://web.archive.org/web/20260503/https://fred.stlouisfed.org/graph/fredgraph.csv?id=NIKKEI225) (2026-04-24 終値, 2026-05-03取得。fred.stlouisfed.org 直接アクセスは HTTP/2 stream INTERNAL_ERROR で失敗) |
| TOPIX | データ取得失敗 | JPX 日次 PDF は取得可だが本作業環境に PDF テキスト抽出ツール無し | [JPX マーケット統計](https://www.jpx.co.jp/markets/statistics-equities/daily/index.html) (取得済 PDF を解析不能, 2026-05-03取得試行) |
| 東証プライム売買代金 (日次平均) | データ取得失敗 | 同上 | [JPX マーケット統計](https://www.jpx.co.jp/markets/statistics-equities/daily/index.html) (取得済 PDF を解析不能, 2026-05-03取得試行) |

### 3.2 業種別騰落（週次、上位・下位各3）

データ取得失敗（JPX 週次集計を保持する PDF からの数値抽出を本作業環境で完了できず）。

### 3.3 日本株イベントカレンダー

- 該当なし

## 4. 差分データ（前週比）

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [`/docs/workflow.md`](/docs/workflow.md) の「差分データ」節を参照）。

### 4.1 閾値超えの変化

閾値は [`/docs/workflow.md`](/docs/workflow.md) の「差分データの閾値（週次）」節を参照。

- 🔺 Major: Brent原油: $98.05 → $111.86 (+14.1%)
- 🔺 Major: WTI原油: $93.40 → $98.42 (+5.4%)
- 🔸 Notable: 日経平均: 58,476 → 59,716.18 (+2.1%)

判定対象外: TOPIX / 東証プライム売買代金 / FedWatch（取得失敗のため判定不能）。

### 4.2 方向履歴と方向反転

- 方向履歴: データ不足（観測対象週数: 2、`world-weekly` 系列が 2 週分のみ）
- 方向反転: データ不足により判定不能

## 5. 次回主要イベント予定

- 2026-04-27 〜 28: 日銀 金融政策決定会合 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)
- 2026-04-28 〜 29: FOMC 会合 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
