---
type: periodic
scope: world
ai-draft: true
published_at: "2026-04-19T10:00:00+09:00"
sources:
  - "https://fred.stlouisfed.org/series/DGS10"
  - "https://www.federalreserve.gov/releases/h15/"
  - "https://www.bls.gov/news.release/cpi.nr0.htm"
  - "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
  - "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
  - "https://www.federalreserve.gov/releases/h10/current/"
  - "https://www.jpx.co.jp/markets/statistics-equities/"
---

# Brief World Weekly: 2026-04-10 world-weekly (us-10y-down)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`/docs/design-principles.md`](/docs/design-principles.md) の「事実と分析の分離」節を参照）

対象期間: 2026-04-06 〜 2026-04-10
観測日: 2026-04-19
市場データの基準日: 2026-04-10 終値（直近営業日）
前週 brief: 該当なし（差分データ初回）
直近の月次 brief: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。

月次統計（CPI / 雇用統計 / 政策金利変更等）はこのファイルでは記録せず、該当月の `macro-monthly` brief を参照する。

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 米10Y利回り | 4.31% | -4bp（前週 4.35%） | [FRED DGS10](https://fred.stlouisfed.org/series/DGS10) (2026-04-19取得) |
| 米2Y利回り | 3.81% | データ取得失敗（履歴CSVアクセス不可） | [Fed H.15 current](https://www.federalreserve.gov/releases/h15/) (2026-04-19取得) |
| 10Y-2Yスプレッド | +50bp | データ取得失敗（前週値不明） | 上記2値より算出 |
| VIX | データ取得失敗 | — | [FRED VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) (アクセス不能, 2026-04-19取得試行) |
| Brent原油 | データ取得失敗 | — | [FRED DCOILBRENTEU](https://fred.stlouisfed.org/series/DCOILBRENTEU) (アクセス不能, 2026-04-19取得試行) |
| WTI原油 | データ取得失敗 | — | [FRED DCOILWTICO](https://fred.stlouisfed.org/series/DCOILWTICO) (アクセス不能, 2026-04-19取得試行) |
| 銅 | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |
| 金 | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |
| FedWatch (4/29-30 据置確率) | データ取得失敗 | — | [CME FedWatch](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) (アクセス不能, 2026-04-19取得試行) |

注: 本 brief 作成時、stlouisfed.org の一部 series は作業環境から取得不能だった。取得できた系列のみ記録し、未取得分は次回 brief 作成時に再試行する。

### 1.2 地政学・グローバルイベント

- 2026-04-10: 米 2026-03 CPI 発表。総合 +3.3% YoY、コア +2.6% YoY。詳細は月次 brief [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md) を参照

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| USD/JPY | 159.22 | データ取得失敗（前週値不明） | [Fed H.10 current](https://www.federalreserve.gov/releases/h10/current/) (2026-04-19取得) |
| EUR/JPY | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |
| AUD/JPY | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |

### 2.2 日本のイベント・速報

- 該当なし（2026-04-06 〜 2026-04-10 の期間にこの brief で追加すべき一次ソース事実は未検出）

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 日経平均 | データ取得失敗 | — | [FRED NIKKEI225](https://fred.stlouisfed.org/series/NIKKEI225) (アクセス不能, 2026-04-19取得試行) |
| TOPIX | データ取得失敗 | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/) (取得未試行, 2026-04-19) |
| 東証プライム売買代金 (日次平均) | データ取得失敗 | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/) (取得未試行, 2026-04-19) |

### 3.2 業種別騰落（週次、上位・下位各3）

データ取得失敗（JPX からの業種別週次騰落率取得手順がこの時点では未確立）。

### 3.3 日本株イベントカレンダー

- 該当なし

## 4. 差分データ（前週比）

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [`/docs/workflow.md`](/docs/workflow.md) の「差分データ」節を参照）。

### 4.1 閾値超えの変化

閾値は [`/docs/workflow.md`](/docs/workflow.md) の「差分データの閾値（週次）」節を参照。

- 米10Y利回り: WoW -4bp（4.35% → 4.31%）、Notable 閾値 ±15bp 未満
- その他指標: データ取得失敗により判定不能

閾値超え: 該当なし（判定可能指標の範囲内）

### 4.2 方向履歴と方向反転

- **方向履歴（過去4週）**: 米10Y利回り: `[↑↑↓↓]`
- **方向反転**: 米10Y利回り: 2026-04-03 に `↑` から `↓` へ反転後、本週は反転後2週目
- その他指標: データ不足または取得失敗により判定不能

## 5. 次回主要イベント予定

- 2026-04-21: 米 2026-03 小売売上高（後日公表、詳細は後続 brief で記録）
- 2026-04-24: 日本 2026-03 全国コア CPI
- 2026-04-27 〜 28: 日銀 金融政策決定会合 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-04-19取得)
- 2026-04-28 〜 29: FOMC 会合 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-04-19取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
