---
type: periodic
scope: world
ai-draft: true | false
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
sources:
  - "URL1"
  - "URL2"
---

# Brief World Daily: YYYY-MM-DD world-daily ({slug})

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的整理のみ。解釈・予測・相場観は書かない）

対象日: YYYY-MM-DD
観測日: YYYY-MM-DD
直近 world-daily brief（あれば）: [YYYY-MM-DD-world-daily-{slug}.md](../MM/YYYY-MM-DD-world-daily-{slug}.md)（存在しない場合は「該当なし」または行ごと省略）
直近の週次 brief: [YYYY-MM-DD-world-weekly-{slug}.md](../MM/YYYY-MM-DD-world-weekly-{slug}.md)（未作成の場合はその旨を明記）
直近の月次 brief: [YYYY-MM-macro-monthly-{slug}.md](../MM/YYYY-MM-macro-monthly-{slug}.md)（未作成の場合はその旨を明記）

`world-daily` は、週次まで待つと stale になる fresh fact を受け止めるための brief。`macro-monthly` がまだ閉じていない月は、当日公表された月次級データを一時的に載せてよい。後日 `macro-monthly` ができたら、その値の正本は `macro-monthly` に移り、この brief は archive として保持する。

## 1. 世界情勢

### 1.1 当日までに増えた一次統計・会合日程

- YYYY-MM-DD: [米国・グローバルの新規事実] [ソース名](URL) (YYYY-MM-DD取得)
- YYYY-MM-DD: [次回会合日程の確定情報] [ソース名](URL) (YYYY-MM-DD取得)

## 2. 日本経済

### 2.1 当日までに増えた一次統計・会合日程

- YYYY-MM-DD: [日本の新規事実] [ソース名](URL) (YYYY-MM-DD取得)
- YYYY-MM-DD: [日銀会合などの日程] [ソース名](URL) (YYYY-MM-DD取得)

## 3. 日本株

### 3.1 当日までに増えた一次ソース事実

- [JPX 等の一次ソース更新があれば記載。なければ「該当なし（当日確認できた一次ソース更新なし）」]

## 4. 次回主要予定

- YYYY-MM-DD: [FOMC / BOJ / 主要統計発表] [ソース名](URL) (YYYY-MM-DD取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
