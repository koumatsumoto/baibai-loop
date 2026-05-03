---
type: periodic
scope: world
ai-draft: true
published_at: "2026-05-03T21:10:00+09:00"
sources:
  - "https://fred.stlouisfed.org/series/DGS10"
  - "https://fred.stlouisfed.org/series/DGS2"
  - "https://fred.stlouisfed.org/series/VIXCLS"
  - "https://fred.stlouisfed.org/series/DCOILBRENTEU"
  - "https://fred.stlouisfed.org/series/DCOILWTICO"
  - "https://fred.stlouisfed.org/series/DEXJPUS"
  - "https://fred.stlouisfed.org/series/NIKKEI225"
  - "https://www.census.gov/retail/sales.html"
  - "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html"
  - "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
  - "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
---

# Brief World Weekly: 2026-04-26 world-weekly (jp-cpi-retail)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`/docs/design-principles.md`](/docs/design-principles.md) の「事実と分析の分離」節を参照）

対象期間: 2026-04-20 〜 2026-04-26
観測日: 2026-05-03
市場データの基準日: 2026-04-24 終値（直近営業日）
前週 brief: [2026-04-19-world-weekly-us-iran-deescalation.md](./2026-04-19-world-weekly-us-iran-deescalation.md)
直近の月次 brief: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。

2026-04-24 に先行 daily brief [2026-04-24-world-daily-jp-cpi-mar-us-retail.md](./2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を作成済み。この週次 brief では同じ月次級データを再掲せず、参照リンクで保持する。

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 米10Y利回り | 4.31% | 前週 brief 表示値 4.31% から 0bp | [FRED DGS10](https://fred.stlouisfed.org/series/DGS10) (2026-05-03取得) |
| 米2Y利回り | 3.78% | 前週 brief 表示値 3.81% から -3bp | [FRED DGS2](https://fred.stlouisfed.org/series/DGS2) (2026-05-03取得) |
| 10Y-2Yスプレッド | +53bp | 前週 brief 表示値 +50bp から +3bp | 上記2値より算出 |
| VIX | 18.71 | 前週 brief 表示値 17.48 から +1.23pt | [FRED VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) (2026-05-03取得) |
| Brent原油 | $111.86 | 前週 brief 表示値 $98.05 から +14.1% | [FRED DCOILBRENTEU](https://fred.stlouisfed.org/series/DCOILBRENTEU) (2026-05-03取得) |
| WTI原油 | $98.42 | 前週 brief 表示値 $93.40 から +5.4% | [FRED DCOILWTICO](https://fred.stlouisfed.org/series/DCOILWTICO) (2026-05-03取得) |
| FedWatch (次回会合 据置確率) | データ取得失敗 | CME ページ取得時に HTTP/2 stream error | [CME FedWatch Tool](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) (アクセス不能, 2026-05-03取得試行) |

### 1.2 地政学・グローバルイベント

- 2026-04-21: 米国 2026-03 小売売上高（Advance）が公表済み。詳細は daily brief [2026-04-24-world-daily-jp-cpi-mar-us-retail.md](./2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を参照。[U.S. Census Bureau](https://www.census.gov/retail/sales.html) (2026-05-03取得)
- 2026-04-28 〜 29: FOMC 会合日程が公表済み。[Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| USD/JPY | 159.35 | 前週 brief 表示値 約159.5 から -0.1% | [FRED DEXJPUS](https://fred.stlouisfed.org/series/DEXJPUS) (2026-05-03取得) |
| EUR/JPY | データ取得失敗 | FRED の該当系列を今回 brief 作成時に確定できず | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-05-03取得試行) |
| AUD/JPY | データ取得失敗 | FRED の該当系列を今回 brief 作成時に確定できず | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-05-03取得試行) |

### 2.2 日本のイベント・速報

- 2026-04-24: 日本 2026-03 全国 CPI が公表済み。詳細は daily brief [2026-04-24-world-daily-jp-cpi-mar-us-retail.md](./2026-04-24-world-daily-jp-cpi-mar-us-retail.md) を参照。[総務省統計局](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) (2026-05-03取得)
- 2026-04-27 〜 28: 日銀 金融政策決定会合の日程が公表済み。[日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 日経平均 | 59,716.18 | 前週 brief 表示値 58,476 から +2.1% | [FRED NIKKEI225](https://fred.stlouisfed.org/series/NIKKEI225) (2026-05-03取得) |
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

- 🔺 Major: Brent原油: $98.05 → $111.86 (+14.1%)
- 🔺 Major: WTI原油: $93.40 → $98.42 (+5.4%)
- 🔸 Notable: 日経平均: 58,476 → 59,716.18 (+2.1%)

### 4.2 方向履歴と方向反転

- 方向履歴: データ不足（観測対象週数: 2）
- 方向反転: データ不足により判定不能

## 5. 次回主要イベント予定

- 2026-04-27 〜 28: 日銀 金融政策決定会合 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-05-03取得)
- 2026-04-28 〜 29: FOMC 会合 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-05-03取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
