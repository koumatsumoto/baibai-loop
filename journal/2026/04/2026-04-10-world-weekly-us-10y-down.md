# World Analysis: 2026-04-10 world-weekly (us-10y-down)

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [../../../docs/design-principles.md](../../../docs/design-principles.md) の「事実と分析の分離」節を参照）

対象期間: 2026-04-06 〜 2026-04-10
観測日: 2026-04-19
市場データの基準日: 2026-04-10 終値（直近営業日）
前週 journal: 該当なし（差分データ初回）
直近の月次 journal: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

階層: **世界情勢 → 日本経済 → 日本株** の順に記録する。

月次統計（CPI / 雇用統計 / 政策金利変更 等）はこのファイルでは記録せず、該当月の `macro-monthly` journal を参照する。

## 1. 世界情勢

### 1.1 グローバルマーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 米 10Y 利回り | 4.31% | -4 bp（前週 4.35%） | [FRED DGS10](https://fred.stlouisfed.org/series/DGS10) (2026-04-19取得) |
| 米 2Y 利回り | 3.81% | データ取得失敗（履歴 CSV アクセス不可） | [Fed H.15 current](https://www.federalreserve.gov/releases/h15/) (2026-04-19取得) |
| 10Y-2Y スプレッド | +50 bp | データ取得失敗（前週値不明） | 上記 2 値より算出 |
| VIX | データ取得失敗 | — | [FRED VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) (アクセス不能, 2026-04-19取得試行) |
| Brent 原油 | データ取得失敗 | — | [FRED DCOILBRENTEU](https://fred.stlouisfed.org/series/DCOILBRENTEU) (アクセス不能, 2026-04-19取得試行) |
| WTI 原油 | データ取得失敗 | — | [FRED DCOILWTICO](https://fred.stlouisfed.org/series/DCOILWTICO) (アクセス不能, 2026-04-19取得試行) |
| 銅 | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |
| 金 | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |
| FedWatch (4/29-30 据え置き確率) | データ取得失敗 | — | [CME FedWatch](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html) (アクセス不能, 2026-04-19取得試行) |

注: 本 journal 作成時、stlouisfed.org 全体が取得環境からブロックされており、FRED/ALFRED 経由の履歴取得は不可。DGS10 のみ初期試行で CSV が取得できた。他指標は次回 journal 作成時に取得再試行。

### 1.2 地政学・グローバルイベント

- 2026-04-10: 米 3 月 CPI 発表、総合 +3.3% YoY・コア +2.6% YoY。詳細は月次 journal [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md) を参照

## 2. 日本経済

### 2.1 為替（クロス円中心）

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| USD/JPY | 159.22 | データ取得失敗（前週値不明） | [Fed H.10 current](https://www.federalreserve.gov/releases/h10/current/) (2026-04-19取得) |
| EUR/JPY | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |
| AUD/JPY | データ取得失敗 | — | [FRED](https://fred.stlouisfed.org/) (アクセス不能, 2026-04-19取得試行) |

### 2.2 日本のイベント・速報

- 該当なし（週次に拾う一次ソース事実は 4/6-4/10 週には未検出。次回 journal 作成時に速報再確認）

## 3. 日本株

### 3.1 マーケット指標

| 指標 | 値 | 週次コメント（前週比） | ソース |
|---|---|---|---|
| 日経平均 | データ取得失敗 | — | [FRED NIKKEI225](https://fred.stlouisfed.org/series/NIKKEI225) (アクセス不能, 2026-04-19取得試行) |
| TOPIX | データ取得失敗 | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/) (取得未試行, 2026-04-19) |
| 東証プライム売買代金 (日次平均) | データ取得失敗 | — | [JPX](https://www.jpx.co.jp/markets/statistics-equities/) (取得未試行, 2026-04-19) |

### 3.2 業種別騰落（週次、上位・下位各 3）

データ取得失敗（JPX 売買代金ページからの業種別週次騰落率取得が今回の環境で未確立）。

### 3.3 日本株イベントカレンダー

- 該当なし（週次に拾う一次ソース事実は未検出）

## 4. 差分データ（前週比）

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データ」節を参照）。

### 4.1 閾値超えの変化

閾値は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データの閾値（週次）」節を参照。

- 米 10Y 利回り: WoW -4 bp (4.35% → 4.31%)、Notable 閾値 ±15 bp 未満
- その他指標: データ取得失敗により判定不能

閾値超え: 該当なし（判定可能指標の範囲内）

### 4.2 方向履歴と方向反転

矢印の判定基準（`↑` = 前週比プラス、`↓` = 前週比マイナス、`→` = 実質変化なし）は workflow 参照。

- **方向履歴（過去 4 週）**: 米 10Y 利回り: `[↑↑↓↓]`（2026-03-20: 4.39→ 2026-03-27: 4.44 (↑)、2026-03-27: 4.44→ 2026-04-03: 4.35 (↓)、2026-04-03: 4.35→ 2026-04-10: 4.31 (↓)、※履歴先頭 2026-03-13→20 は +0.11%、最左の ↑）
- **方向反転**: 米 10Y 利回り: 2026-03-06 以降 4 週連続 ↑（3/6, 3/13, 3/20, 3/27）の後、2026-04-03 に ↓ へ反転（`[↑↑↑↑↓]` の最終週 = 2026-04-03）。本週（2026-04-10）は反転後の 2 週目
- その他指標: データ不足または取得失敗により判定不能

## 5. 次回主要イベント予定

- 2026-04-21: 米 3 月小売売上高（Census MARTS、従来 4/16 から延期）
- 2026-04-24 または -25: 日本 3 月全国コア CPI（総務省）
- 2026-04-28 〜 29: FOMC 金融政策決定会合 [Fed FOMC Calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-04-19取得)
- 2026-04-27 〜 28: 日銀 金融政策決定会合 [BOJ 金融政策決定会合](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-04-19取得)
- 2026-04-28 前後: 日本 3 月 完全失業率・有効求人倍率
- 2026-04-30: 日本 3 月 鉱工業生産、米 3 月コア PCE

---

記入ルールは [../../../docs/workflow.md](../../../docs/workflow.md) を、設計根拠は [../../../docs/design-principles.md](../../../docs/design-principles.md) を参照。
