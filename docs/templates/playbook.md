---
playbook_id: "playbook-slug-v1"
version: 1
horizon: "5-40 営業日"
playbook_type: primary | supplementary
status: active | deprecated
updated_at: "YYYY-MM-DDTHH:MM:SS+09:00"
---

# Playbook: [playbook 名]

**成分**: `records/_playbooks/` 配下の運用資産。[`/docs/components/research.md`](/docs/components/research.md) の `playbook` front matter で参照される。

## 1. 概要

### 1.1 対象

- [どんな銘柄を対象にするか、1-2 段落で]

### 1.2 狙い

- [この playbook が狙う alpha の性質、1-2 段落]

### 1.3 保有期間

- 5〜40 営業日（swing の範囲内）

### 1.4 Type

- **Primary**: 本命 playbook（複数 thesis に適用可）
- **Supplementary**: 補助 playbook（特定条件下でのみ適用）

## 2. 判定条件（entry 前の必須条件）

### 2.1 Valuation 条件

- [PER / PBR / EV-EBITDA 等の具体的閾値]

### 2.2 Mean-Reversion 条件

- [一時的割安の根拠となる条件]

### 2.3 Catalyst 条件（supplementary の場合のみ）

- [短期 catalyst の種別と freshness 上限]

### 2.4 Macro gate 条件

- 業種/地域のマクロ gate が `tailwind` または `neutral`
- `headwind` は採用不可

### 2.5 Universe 条件

- 時価総額 200 億円以上 + 売買代金 3 億円以上（[`/docs/screening/universe-rules.md`](/docs/screening/universe-rules.md)）
- 200-500 億円帯の特例条件（該当する場合のみ）

## 3. 修飾因子

### 3.1 Crowding

- 空売り残高 / 日々公表信用 / 特別注意 / 貸借状態の評価
- 踏み上げリスクと逆回転リスクの両面評価

### 3.2 Macro tailwind（二重確認）

- outlook の業種/地域判定を再確認

### 3.3 Relative strength

- 業種 RS と個別 RS の整合性

## 4. 無効化条件

- [業績下方修正]
- [業種中央値切り下がり]
- [マクロゲート headwind 反転]
- [出来高を伴わない下落継続]

## 5. Exit 戦略

- **利確目標**: [利益率 or 価格レンジ]
- **損切り**: [損失許容率]
- **時間切れ**: 最長 40 営業日
- **無効化 exit**: 無効化条件が一つでも発生したら exit

## 6. Position sizing

- 時価総額別上限（[`/docs/screening/universe-rules.md`](/docs/screening/universe-rules.md)）に従う
- 1,000 億円以上: 2%、500-1,000 億円: 1%、200-500 億円: 0.5%（P-B のみ）

## 7. Kill switch 確認

- 決算またぎ禁止
- 日銀会合前日禁止
- FOMC 前日禁止

## 8. AI の役割境界

[`/docs/components/research.md`](/docs/components/research.md) の AI 境界表を継承。核心:

- **AI 可**: Thesis / valuation / 仮説ドラフト / catalyst / price / crowding
- **人間のみ**: Macro gate 確定 / 一次ソース URL 確認 / 最終採用判定

## 9. 改訂履歴

| 版 | 日付 | 変更内容 | 判断根拠 |
| --- | --- | --- | --- |
| v1 | YYYY-MM-DD | 初版 | [サンプル数、retro feedback 等] |
