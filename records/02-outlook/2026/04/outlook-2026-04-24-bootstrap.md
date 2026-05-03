---
ai-draft: true
published_at: "2026-04-27T09:00:00+09:00"
horizon: "1-6m"
updated_from:
  - records/01-brief/2026/01/2026-01-macro-monthly-overview.md
  - records/01-brief/2026/02/2026-02-macro-monthly-jp-core-cpi-sub2.md
  - records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.md
  - records/01-brief/2026/04/2026-04-10-world-weekly-us-10y-down.md
  - records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md
  - records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md
sectors:
  "水産・農林業": neutral
  "鉱業": neutral
  "建設業": neutral
  "食料品": neutral
  "繊維製品": neutral
  "パルプ・紙": neutral
  "化学": neutral
  "医薬品": neutral
  "石油・石炭製品": neutral
  "ゴム製品": neutral
  "ガラス・土石製品": neutral
  "鉄鋼": neutral
  "非鉄金属": neutral
  "金属製品": neutral
  "機械": neutral
  "電気機器": neutral
  "輸送用機器": neutral
  "精密機器": neutral
  "その他製品": neutral
  "電気・ガス業": neutral
  "陸運業": neutral
  "海運業": neutral
  "空運業": neutral
  "倉庫・運輸関連業": neutral
  "情報・通信業": neutral
  "卸売業": neutral
  "小売業": neutral
  "銀行業": neutral
  "証券、商品先物取引業": neutral
  "保険業": neutral
  "その他金融業": neutral
  "不動産業": neutral
  "サービス業": neutral
regions:
  us: neutral
  japan-domestic: neutral
  japan-external-demand: tailwind
  emerging: null
---

# Outlook: 2026-04-24 bootstrap

**成分**: 4 成分アーキテクチャの **(c) マクロ見解**（[`/docs/components/outlook.md`](/docs/components/outlook.md)）

**レイヤー**: 分析レイヤー（解釈 OK。ただし根拠となる brief への参照必須）

Horizon: 1-6 か月

Bootstrap（v1 初回）: stale な履歴だけではなく、2026-04-24 時点で追加した daily brief まで含めて初回 outlook を作成した。

## 1. Executive Summary

2026-04-24 時点のマクロは、米国では 2026-03 CPI の再加速と 2026-03 小売売上高の強さが同居し、日本では全国コア CPI が +1.8% と 2%近辺を維持しつつ、USD/JPY は 159 台、原油は 100ドル割れまで低下している。1-6か月 horizon では、BoJ / FOMC 前のため sector は広く `neutral` を維持し、横断的な円安と外需の強さは `regions.japan-external-demand` に集約して表現する。

## 2. 主要 brief の要点集約

- [records/01-brief/2026/01/2026-01-macro-monthly-overview.md](/records/01-brief/2026/01/2026-01-macro-monthly-overview.md): Fed / BoJ ともに 2026-01 は据置で始まり、日本コア CPI は 2.0% と目標水準
- [records/01-brief/2026/02/2026-02-macro-monthly-jp-core-cpi-sub2.md](/records/01-brief/2026/02/2026-02-macro-monthly-jp-core-cpi-sub2.md): 日本コア CPI は 1.6% まで鈍化、米雇用は弱含み
- [records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.md](/records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.md): 米 CPI は +3.3% YoY に再加速し、同月の米小売売上高は +1.7% MoM
- [records/01-brief/2026/04/2026-04-10-world-weekly-us-10y-down.md](/records/01-brief/2026/04/2026-04-10-world-weekly-us-10y-down.md): 米10Y は 4.31% まで低下し、4月末の BoJ / FOMC が次の主要トリガーとして残る
- [records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md](/records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md): Brent / WTI は低下し、USD/JPY は 159 台、日経平均は高値圏で推移
- [records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md): 日本 2026-03 全国コア CPI +1.8%、コアコア +2.4%、次回 BoJ / FOMC 日程が直近に迫る

## 3. 業種別判定の根拠

### 3.1 tailwind 判定業種

該当なし。円安と米最終需要の強さは輸出関連に横断的に効くが、この bootstrap では `regions.japan-external-demand` に集約し、sector 側は業種固有の追加根拠が出るまで `neutral` を維持する。

### 3.2 neutral 判定業種

以下を代表例とし、front matter で `tailwind` を付けていない残り 30 業種はすべて `neutral` とした。理由は、現時点の brief 群だけでは decisive な上振れ / 下振れの根拠が不足しているため。

| 業種 | 判定 | 根拠 | 根拠 brief |
| --- | --- | --- | --- |
| 機械 | neutral | 円安・外需は追い風だが、同根拠は `japan-external-demand` 側で表現する。業種固有の追加 signal は未確認 | [2026-03 monthly](/records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| 電気機器 | neutral | 輸出需要の強さは地域軸に寄せ、sector として一段強気にする固有根拠はまだ不足 | [2026-04-19 weekly](/records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| 輸送用機器 | neutral | 円安と原油低下は確認できるが、bootstrap 時点では sector 固有 tailwind まで断定しない | [2026-04-19 weekly](/records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| 銀行業 | neutral | BoJ は 0.75% 維持で、次回会合前。金利上昇再加速を front-run する根拠が不足 | [2026-01 monthly](/records/01-brief/2026/01/2026-01-macro-monthly-overview.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| 小売業 | neutral | 日本コア CPI は 1.8% だが、消費の強弱を sector headwind まで断定できる brief が不足 | [2026-02 monthly](/records/01-brief/2026/02/2026-02-macro-monthly-jp-core-cpi-sub2.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| 不動産業 | neutral | 国内インフレは 2%近辺、政策金利は据置。金利・賃料のどちらも明確方向が出ていない | [2026-01 monthly](/records/01-brief/2026/01/2026-01-macro-monthly-overview.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| 情報・通信業 | neutral | マクロから直接の sector signal が弱く、個別要因優位とみなす | [2026-03 monthly](/records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.md) |
| 海運業 | neutral | 原油低下は確認できるが、運賃サイクルや貿易量の decisive signal を brief では未確認 | [2026-04-19 weekly](/records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md) |

### 3.3 headwind 判定業種

該当なし（初回 bootstrap では conservative に `neutral` を厚く残した）。

## 4. 地域別判定の根拠

| 地域 | 判定 | 根拠 | 根拠 brief |
| --- | --- | --- | --- |
| us | neutral | CPI 再加速は逆風だが、小売売上高は強い。相殺して neutral | [2026-03 monthly](/records/01-brief/2026/03/2026-03-macro-monthly-us-cpi-3p3.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| japan-domestic | neutral | 日本コア CPI は +1.8%、政策金利は 0.75% 維持。需要加速・減速のどちらにもまだ振れない | [2026-02 monthly](/records/01-brief/2026/02/2026-02-macro-monthly-jp-core-cpi-sub2.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| japan-external-demand | tailwind | USD/JPY 159台と米最終需要の強さが輸出主導に有利 | [2026-04-19 weekly](/records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md), [2026-04-24 daily](/records/01-brief/2026/04/2026-04-24-world-daily-jp-cpi-mar-us-retail.md) |
| emerging | null | 参照 brief 群に emerging を直接判定する材料が不足 | [2026-04-19 weekly](/records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.md) |

## 5. 変化ポイント（前回 outlook からの差分）

該当なし（初回作成）。

## 6. 次回更新 trigger の想定

- 2026-04-27 〜 28: 日銀 金融政策決定会合
- 2026-04-28 〜 29: FOMC 会合
- 2026-04-28前後: 日本 2026-03 完全失業率・有効求人倍率
- 2026-04-30: 米 コアPCE、日本 2026-03 鉱工業生産

---

記入ルールは [`/docs/components/outlook.md`](/docs/components/outlook.md) を参照。
