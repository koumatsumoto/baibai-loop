---
type: periodic
scope: world
ai-draft: true
published_at: "2026-04-19T18:00:00+09:00"
sources:
  - "https://www.bls.gov/news.release/cpi.nr0.htm"
  - "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-t.html"
  - "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260318a.htm"
  - "https://www.boj.or.jp/about/press/kaiken_2026/kk260126a.pdf"
  - "https://fred.stlouisfed.org/series/NIKKEI225"
  - "https://fred.stlouisfed.org/series/DEXJPUS"
  - "https://fred.stlouisfed.org/series/DGS10"
  - "https://fred.stlouisfed.org/series/DGS2"
  - "https://fred.stlouisfed.org/series/VIXCLS"
  - "https://fred.stlouisfed.org/series/DCOILBRENTEU"
  - "https://fred.stlouisfed.org/series/DCOILWTICO"
  - "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
  - "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
  - "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
  - "https://www.cnbc.com/2026/04/17/oil-prices-wti-brent-israel-lebanon-ceasefire-trump.html"
---

# Brief World Weekly: 2026-04-19 world-weekly (us-iran-deescalation)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`/docs/design-principles.md`](/docs/design-principles.md) の「事実と分析の分離」節を参照）

対象期間: 2026-04-13 〜 2026-04-19
観測日: 2026-04-19
市場データの基準日: 2026-04-17 終値（直近営業日）
前週 brief: [2026-04-10-world-weekly-us-10y-down.md](./2026-04-10-world-weekly-us-10y-down.md)
直近の月次 brief: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。

月次統計（CPI / 雇用統計 / 政策金利変更等）はこのファイルでは記録せず、該当月の `macro-monthly` brief を参照する。

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 米10Y利回り | 4.31% | 2026-04-10 値、前週 brief と同値 | [FRED DGS10](https://fred.stlouisfed.org/series/DGS10) (2026-04-19取得) |
| 米2Y利回り | 3.81% | 2026-04-10 値 | [FRED DGS2](https://fred.stlouisfed.org/series/DGS2) (2026-04-19取得) |
| 10Y-2Yスプレッド | +50bp | 上記2値より算出 | 上記2値より算出 |
| VIX | 17.48 | 落ち着き水準 | [FRED VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) (2026-04-19取得) |
| Brent原油 | $98.05 | 月初 $100超から低下 | [FRED DCOILBRENTEU](https://fred.stlouisfed.org/series/DCOILBRENTEU) (2026-04-19取得) |
| WTI原油 | $93.40 | 同上 | [FRED DCOILWTICO](https://fred.stlouisfed.org/series/DCOILWTICO) (2026-04-19取得) |
| FedWatch (次回会合 据置確率) | 97.9% | 4/29-30 FOMC 据置をほぼ織り込み | [CME FedWatch Tool](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) (2026-04-19取得) |

### 1.2 地政学・グローバルイベント

- 2026-04-17: Brent 原油は $98.05、WTI は $93.40。Reuters / AP ではなく暫定で `[補助外]` として [CNBC](https://www.cnbc.com/2026/04/17/oil-prices-wti-brent-israel-lebanon-ceasefire-trump.html) (2026-04-19取得) を記録
- 2026-04-10: 米 2026-03 CPI 発表。詳細は月次 brief [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md) を参照
- 2026-03-18: FOMC は政策金利目標レンジ 3.50-3.75% を据置 [Federal Reserve FOMC Statement 2026-03-18](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260318a.htm) (2026-04-19取得)

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| USD/JPY | 約159.5 | 3月末高値 160.26 から低下 | [FRED DEXJPUS](https://fred.stlouisfed.org/series/DEXJPUS) (2026-04-19取得) |
| EUR/JPY | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |
| AUD/JPY | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |

### 2.2 日本のイベント・速報

- 2026-03-31公表分の東京都区部 CPI: コア +1.7%、コアコア +2.3% [総務省統計局 CPI 東京都区部](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-t.html) (2026-04-19取得)
- 2026-01-26: 日銀は無担保コールレート誘導目標 0.75% を維持 [BOJ 総裁記者会見 2026-01-26](https://www.boj.or.jp/about/press/kaiken_2026/kk260126a.pdf) (2026-04-19取得)

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 日経平均 | 58,476 | 2026-04-17 終値、当日 -1.75% | [FRED NIKKEI225](https://fred.stlouisfed.org/series/NIKKEI225) (2026-04-19取得) |
| TOPIX | データ取得失敗 | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/) (取得未試行, 2026-04-19) |
| 東証プライム売買代金 (日次平均) | データ取得失敗 | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/) (取得未試行, 2026-04-19) |

### 3.2 業種別騰落（週次、上位・下位各3）

データ取得失敗（JPX 週次集計の一次取得をこの時点では未反映）。

### 3.3 日本株イベントカレンダー

- 該当なし

## 4. 差分データ（前週比）

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [`/docs/workflow.md`](/docs/workflow.md) の「差分データ」節を参照）。

### 4.1 閾値超えの変化

閾値は [`/docs/workflow.md`](/docs/workflow.md) の「差分データの閾値（週次）」節を参照。

- 原油価格の低下は記録したが、前週値をこの brief では未算出
- その他、閾値超えの判定に必要な前週データは不足または取得失敗

閾値超え: 判定不能または該当なし

### 4.2 方向履歴と方向反転

- 米10Y利回り: 直近の方向履歴は前週 brief を参照
- その他指標: 過去4週データ不足または取得失敗により判定不能

## 5. 次回主要イベント予定

- 2026-04-24: 日本 2026-03 全国コア CPI
- 2026-04-27 〜 28: 日銀 金融政策決定会合 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-04-19取得)
- 2026-04-28 〜 29: FOMC 会合 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-04-19取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
