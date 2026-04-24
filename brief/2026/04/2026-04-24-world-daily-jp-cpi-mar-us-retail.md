---
type: periodic
scope: world
ai-draft: true
published_at: "2026-04-24T19:00:00+09:00"
sources:
  - "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html"
  - "https://www.census.gov/retail/sales.html"
  - "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
  - "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
---

# Brief World Daily: 2026-04-24 world-daily (jp-cpi-mar-us-retail)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的整理のみ。解釈・予測・相場観は書かない）

対象日: 2026-04-24
観測日: 2026-04-24
直近 world-daily brief: 該当なし
直近の週次 brief: [2026-04-19-world-weekly-us-iran-deescalation.md](./2026-04-19-world-weekly-us-iran-deescalation.md)
直近の月次 brief: [../03/2026-03-macro-monthly-us-cpi-3p3.md](../03/2026-03-macro-monthly-us-cpi-3p3.md)

`world-daily` は、週次まで待つと stale になる fresh fact を受け止めるための brief。2026-04-24 時点では、当月 `macro-monthly` がまだ閉じていないため、当日までに公表された月次級データを一時的にここへ保持する。今回の CPI / retail sales は routine 公表であり、閾値超え surprise ではないため `event` ではなく `world-daily` に置く。

## 1. 世界情勢

### 1.1 当日までに増えた一次統計・会合日程

- 2026-04-21: 米国 2026-03 小売売上高（Advance）は 752.1 billion dollars、前月比 +1.7%、前年比 +4.0%。Retail trade sales は前月比 +1.9%。[U.S. Census Bureau](https://www.census.gov/retail/sales.html) (2026-04-24取得)
- 2026-04-28 〜 29: FOMC 会合日程が公表済み。[Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-04-24取得)

## 2. 日本経済

### 2.1 当日までに増えた一次統計・会合日程

- 2026-04-24: 日本 2026-03 全国 CPI が公表。総合指数は 112.7（前年同月比 +1.5%）、生鮮食品を除く総合指数は 112.1（同 +1.8%）、生鮮食品及びエネルギーを除く総合指数は 111.9（同 +2.4%）。[総務省統計局](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) (2026-04-24取得)
- 2026-04-27 〜 28: 日銀 金融政策決定会合の日程が公表済み。[日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-04-24取得)

## 3. 日本株

### 3.1 当日までに増えた一次ソース事実

- 該当なし（当日確認できた JPX 一次ソースの新規反映対象は本 brief 作成範囲では未採用）

## 4. 次回主要予定

- 2026-04-27 〜 28: 日銀 金融政策決定会合 [日本銀行](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-04-24取得)
- 2026-04-28 〜 29: FOMC 会合 [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-04-24取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
