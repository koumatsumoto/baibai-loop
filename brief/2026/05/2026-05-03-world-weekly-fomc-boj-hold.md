---
type: periodic
scope: world
ai-draft: true
published_at: "2026-05-03T21:00:00+09:00"
sources:
  - "https://fred.stlouisfed.org/series/DGS10"
  - "https://fred.stlouisfed.org/series/DGS2"
  - "https://fred.stlouisfed.org/series/VIXCLS"
  - "https://fred.stlouisfed.org/series/DCOILBRENTEU"
  - "https://fred.stlouisfed.org/series/DCOILWTICO"
  - "https://fred.stlouisfed.org/series/DEXJPUS"
  - "https://fred.stlouisfed.org/series/NIKKEI225"
  - "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm"
  - "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
  - "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
---

# Brief World Weekly: 2026-05-03 world-weekly (fomc-boj-hold)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`/docs/design-principles.md`](/docs/design-principles.md) の「事実と分析の分離」節を参照）

対象期間: 2026-04-27 〜 2026-05-03
観測日: 2026-05-03
市場データの基準日: 2026-04-30 終値（米金利・VIX） / 2026-04-27 終値（原油） / 2026-05-01 終値（日経平均）
前週 brief: [2026-04-26-world-weekly-jp-cpi-retail.md](../04/2026-04-26-world-weekly-jp-cpi-retail.md)
直近の月次 brief: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。

月次統計（CPI / 雇用統計 / 政策金利変更等）はこのファイルでは原則記録せず、該当月の `macro-monthly` brief または先行 `world-daily` を参照する。2026-04-24 公表の日本 2026-03 全国 CPI と米 2026-03 小売売上高は、先行 daily brief [../04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md](../04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を参照。

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 米10Y利回り | 4.40% | 前週 brief 表示値 4.31% から +9bp | [FRED DGS10](https://fred.stlouisfed.org/series/DGS10) (2026-05-03取得) |
| 米2Y利回り | 3.88% | 前週 brief 表示値 3.78% から +10bp | [FRED DGS2](https://fred.stlouisfed.org/series/DGS2) (2026-05-03取得) |
| 10Y-2Yスプレッド | +52bp | 前週 brief 表示値 +53bp から -1bp | 上記2値より算出 |
| VIX | 16.89 | 前週 brief 表示値 18.71 から -1.82pt | [FRED VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) (2026-05-03取得) |
| Brent原油 | $113.89 | 2026-04-27 値。前週 brief 表示値 $111.86 から +1.8% | [FRED DCOILBRENTEU](https://fred.stlouisfed.org/series/DCOILBRENTEU) (2026-05-03取得) |
| WTI原油 | $99.89 | 2026-04-27 値。前週 brief 表示値 $98.42 から +1.5% | [FRED DCOILWTICO](https://fred.stlouisfed.org/series/DCOILWTICO) (2026-05-03取得) |
| FedWatch (次回会合 据置確率) | データ取得失敗 | CME ページ取得時に HTTP/2 stream error | [CME FedWatch Tool](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) (アクセス不能, 2026-05-03取得試行) |

### 1.2 地政学・グローバルイベント

- 2026-04-29: FOMC は政策金利目標レンジ 3.50% 〜 3.75% を維持。[Federal Reserve FOMC Statement](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm) (2026-05-03取得)
- 2026-04-29: FOMC は 2026-04-28 〜 29 会合の声明文、Implementation Note、記者会見資料を公表。[Federal Reserve FOMC Calendars](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| USD/JPY | 159.35 | 前週 brief 表示値 約159.5 から -0.1% | [FRED DEXJPUS](https://fred.stlouisfed.org/series/DEXJPUS) (2026-05-03取得) |
| EUR/JPY | データ取得失敗 | FRED の該当系列を今回 brief 作成時に確定できず | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-05-03取得試行) |
| AUD/JPY | データ取得失敗 | FRED の該当系列を今回 brief 作成時に確定できず | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-05-03取得試行) |

### 2.2 日本のイベント・速報

- 2026-04-27 〜 28: 日銀 金融政策決定会合が開催され、金融市場調節方針に関する公表文と展望レポートが 2026-04-28 に公表済み。[日本銀行 金融政策決定会合の運営](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)
- 2026-05-12: 日銀 2026-04-27 〜 28 会合の「主な意見」公表予定。[日本銀行 金融政策決定会合の運営](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)
- 2026-06-19: 日銀 2026-04-27 〜 28 会合の議事要旨公表予定。[日本銀行 金融政策決定会合の運営](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 日経平均 | 59,513.12 | 前週 brief 表示値 59,716.18 から -0.3% | [FRED NIKKEI225](https://fred.stlouisfed.org/series/NIKKEI225) (2026-05-03取得) |
| TOPIX | データ取得失敗 | JPX ページから当該日の指数値を今回 brief 作成時に確定できず | [JPX マーケット統計](https://www.jpx.co.jp/markets/statistics-equities/) (アクセス不能, 2026-05-03取得試行) |
| 東証プライム売買代金 (日次平均) | データ取得失敗 | JPX ページから週次平均を今回 brief 作成時に確定できず | [JPX マーケット統計](https://www.jpx.co.jp/markets/statistics-equities/) (アクセス不能, 2026-05-03取得試行) |

### 3.2 業種別騰落（週次、上位・下位各3）

データ取得失敗（JPX 週次集計の一次取得をこの時点では未反映）。

### 3.3 日本株イベントカレンダー

- 該当なし

## 4. 差分データ（前週比）

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [`/docs/workflow.md`](/docs/workflow.md) の「差分データ」節を参照）。

### 4.1 閾値超えの変化

閾値は [`/docs/workflow.md`](/docs/workflow.md) の「差分データの閾値（週次）」節を参照。

閾値超え: 該当なし（判定可能指標の範囲内）

### 4.2 方向履歴と方向反転

- 方向履歴: データ不足（観測対象週数: 3）
- 方向反転: データ不足により判定不能

## 5. 次回主要イベント予定

- 2026-05-12: 日銀 2026-04-27 〜 28 会合の「主な意見」公表予定 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)
- 2026-06-16 〜 17: FOMC 会合 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)
- 2026-06-19: 日銀 2026-04-27 〜 28 会合の議事要旨公表予定 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
