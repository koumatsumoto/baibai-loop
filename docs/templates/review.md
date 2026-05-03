---
ticker: "XXXX"
trade_ref: records/05-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
research_ref: records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
playbook: valuation-mean-reversion-v1 | valuation-catalyst-confirmation-v1
entry_date: "YYYY-MM-DD"
exit_date: "YYYY-MM-DD"
pnl_pct: 数値
review_15d_done: true | false
review_30d_done: true | false
failure_class: null | "材料誤読" | "既に織り込み済み" | "マクロ逆風" | "混雑" | "流動性不足" | "ルール違反"
success_class: null | "仮説的中" | "catalyst 反応" | "macro tailwind" | "timing 一致"
free_text: "一行で事後検証の要点"
---

# Review: YYYY-MM-DD XXXX [銘柄名]

**成分**: 4 成分アーキテクチャの下流 **reviews**（[`/docs/components/reviews.md`](/docs/components/reviews.md)）

**Trade**: [records/05-trades/YYYY/MM/YYYY-MM-DD-XXXX.md](/records/05-trades/YYYY/MM/YYYY-MM-DD-XXXX.md)
**Research**: [records/04-research/YYYY/MM/YYYY-MM-DD-XXXX-*.md](/records/04-research/YYYY/MM/YYYY-MM-DD-XXXX-*.md)

## 1. Trade 概要

- Playbook: [valuation-mean-reversion-v1 | valuation-catalyst-confirmation-v1]
- Entry: YYYY-MM-DD @XXXXX 円
- Exit: YYYY-MM-DD @XXXXX 円
- 損益率: +X.X%
- 保有営業日: XX 日

## 2. 採用時 thesis の振り返り

- **原 thesis**（research から）: [1-2 段落転写]
- **実際に起きたこと**: [事実として記述]
- **Thesis の的中度**: [完全的中 / 部分的中 / 外れ]

## 3. 成功分類 or 失敗分類

### 3.1 分類

- **失敗分類**（該当する場合）: [材料誤読 / 織り込み済み / マクロ逆風 / 混雑 / 流動性不足 / ルール違反] または null
- **成功分類**（該当する場合）: [仮説的中 / catalyst 反応 / macro tailwind / timing 一致] または null
- 両方 null は許容しない（どちらか記入必須）

### 3.2 自由記述（必須）

[1 行で事後検証の要点。四半期再分類の source]

## 4. +15 営業日レビュー（exit 日 + 15 営業日時点）

Review 実施日: YYYY-MM-DD

- exit 後の株価推移: [+X%, 上昇/下降トレンド継続/反転]
- 同業種の推移: [業種全体との相対パフォーマンス]
- マクロ環境の変化: [outlook に関連する変化があれば記録]
- 振り返り: [exit タイミングは適切だったか、早すぎた/遅すぎた]

## 5. +30 営業日レビュー（exit 日 + 30 営業日時点）

Review 実施日: YYYY-MM-DD

- exit 後の株価推移: [+X%]
- 1 か月後の整理: [thesis の妥当性、他銘柄との比較]
- Playbook へのフィードバック候補: [次周回で変更すべきポイント]

## 6. 月次 retro への引き渡し

本 review の要点を月次 retro ([`retro-YYYYMM.md`](/records/06-reviews/YYYY/retro-YYYYMM.md)) でまとめる:

- 成功/失敗分類と自由記述
- 四半期再分類の input 候補
- Playbook 改訂提案（あれば）

---

参照: [`/docs/components/reviews.md`](/docs/components/reviews.md), [`/docs/screening/failure-taxonomy.md`](/docs/screening/failure-taxonomy.md)
