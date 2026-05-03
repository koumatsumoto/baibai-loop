---
ai-draft: true | false
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
horizon: "1-6m"
updated_from:
  - records/01-brief/YYYY/MM/world-daily-*.md
  - records/01-brief/YYYY/MM/world-weekly-*.md
  - records/01-brief/YYYY/MM/*-macro-monthly-*.md
sectors:
  "情報・通信": tailwind | neutral | headwind | null
  "銀行": tailwind | neutral | headwind | null
  "不動産": tailwind | neutral | headwind | null
  # 東証 33 業種の正式名称を使う
regions:
  us: tailwind | neutral | headwind | null
  japan-domestic: tailwind | neutral | headwind | null
  japan-external-demand: tailwind | neutral | headwind | null
  emerging: tailwind | neutral | headwind | null
---

# Outlook: YYYY-MM-DD {slug}

**成分**: 4 成分アーキテクチャの **(c) マクロ見解**（[`/docs/components/outlook.md`](/docs/components/outlook.md)）

**レイヤー**: 分析レイヤー（解釈 OK。ただし根拠となる brief への参照必須）

Horizon: 1-6 か月

Bootstrap（v1 初回）: [`/docs/components/outlook.md`](/docs/components/outlook.md) の Bootstrap 規則に従って作成。既存 brief だけで stale なら、先に `world-daily` / `event` を追加してから `updated_from` に含める。保守的に neutral を多めに記入し、横断的要因は `regions` 側へ寄せる。

## 1. Executive Summary

1 段落で現在のマクロ見解を要約。何が追い風で何が逆風かを明示。

## 2. 主要 brief の要点集約

`updated_from` に挙げた各 brief のどこが effective だったかを記述:

- [records/01-brief/YYYY/MM/world-weekly-YYYY-MM-DD-*.md]: [要点 1-2 行]
- [records/01-brief/YYYY/MM/YYYY-MM-macro-monthly-*.md]: [要点 1-2 行]

## 3. 業種別判定の根拠

### 3.1 tailwind 判定業種

| 業種 | 判定 | 根拠 | 根拠 brief |
| --- | --- | --- | --- |
| [業種名] | tailwind | [マクロ追い風要因の簡潔な説明] | [brief path] |

横断的な円安・外需要因だけで説明できる場合は、`sectors` ではなく `regions.japan-external-demand` 側で表現する。

### 3.2 neutral 判定業種

| 業種 | 判定 | 根拠 | 根拠 brief |
| --- | --- | --- | --- |
| [業種名] | neutral | [判定根拠] | [brief path] |

### 3.3 headwind 判定業種

| 業種 | 判定 | 根拠 | 根拠 brief |
| --- | --- | --- | --- |
| [業種名] | headwind | [マクロ逆風要因の簡潔な説明] | [brief path] |

## 4. 地域別判定の根拠

| 地域 | 判定 | 根拠 | 根拠 brief |
| --- | --- | --- | --- |
| us | [判定] | [根拠] | [brief path] |
| japan-domestic | [判定] | [根拠] | [brief path] |

## 5. 変化ポイント（前回 outlook からの差分）

前回 outlook との比較で判定が変わった業種 / 地域を列挙:

- [業種/地域]: [前回判定] → [今回判定]、根拠: [brief path]

前回 outlook がない場合（Bootstrap 等）は「該当なし（初回作成）」と記載。

## 6. 次回更新 trigger の想定

次に outlook を更新すべきイベントを列挙:

- YYYY-MM-DD: [FOMC / BOJ / CPI 発表等]
- [その他の想定 trigger]

---

記入ルールは [`/docs/components/outlook.md`](/docs/components/outlook.md) を参照。
