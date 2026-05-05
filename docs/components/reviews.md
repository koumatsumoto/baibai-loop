# components/reviews.md

Baibai-Loop 4 成分アーキテクチャの下流 **reviews** 成分の運用仕様。事後検証と月次 retro で feedback loop を閉じる。全体構造は [`../architecture.md`](../architecture.md) を参照。

## 1. 役割

- `records/05-trades/` の完結（exit 済み）に対し、**決済後 +15 / +30 営業日レビュー** を作成
- **月次 retro** で成功/失敗分類、skipped trade log、playbook 改訂判断
- retro からの feedback を playbook / screening / outlook 運用に反映

## 2. 種類

### 2.1 個別 review

- trade 1 件ごとに作成
- 決済後 +15 営業日、+30 営業日の 2 時点で追記
- 決済前 exit（kill switch 適用 etc.）も必須記録

### 2.2 月次 retro

- 毎月 1 回（月末または翌月初）
- 当月の全個別 review + skipped trade log を集約
- 失敗分類の再集計、playbook 改訂判断、次周回の変更点

## 3. Path と命名

```
records/06-reviews/YYYY/MM/YYYY-MM-DD-<ticker>.md        # 個別 review（exit 日付）
records/06-reviews/YYYY/retro-YYYYMM.md                  # 月次 retro
```

## 4. 個別 review の Front matter

```yaml
---
ticker: "7203"
trade_ref: records/05-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md  # 必須
research_ref: records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md  # 必須
playbook: valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth
entry_date: "YYYY-MM-DD"
exit_date: "YYYY-MM-DD"
pnl_pct: 数値
review_15d_done: true | false
review_30d_done: true | false
failure_class: null | "材料誤読" | "既に織り込み済み" | "マクロ逆風" | "混雑" | "流動性不足" | "ルール違反"
success_class: null | "仮説的中" | "catalyst 反応" | "macro tailwind" | "timing 一致"
free_text: "一行で事後検証の要点"  # 必須（自由記述）
---
```

- `failure_class` または `success_class` のいずれかを記入（両方 null は許容しない）
- `free_text` は失敗/成功分類を選んでも必須（四半期再分類の source）

## 5. 月次 retro の Front matter

```yaml
---
retro_month: "YYYY-MM"
total_trades: 整数
open_trades: 整数
closed_trades: 整数
skipped_candidates: 整数              # 見送り + 保留の合計
wins: 整数                            # pnl_pct > 0
losses: 整数                          # pnl_pct < 0
pnl_pct_sum: 数値
failure_class_counts:
  材料誤読: 整数
  既に織り込み済み: 整数
  マクロ逆風: 整数
  混雑: 整数
  流動性不足: 整数
  ルール違反: 整数                    # 別枠、playbook 改訂の input にしない
success_class_counts:
  仮説的中: 整数
  catalyst 反応: 整数
  macro tailwind: 整数
  timing 一致: 整数
playbook_revision_decision: "据え置き" | "小改訂" | "大幅改訂"
next_cycle_changes:
  - "変更点 1"
  - "変更点 2"
price_missing_counts:
  plus_15bd: 整数
  plus_30bd: 整数
---
```

## 6. 失敗分類（固定 6 種）

個別 review で `failure_class` に記入する分類:

| 分類 | 定義 |
| --- | --- |
| 材料誤読 | 一次材料の解釈が誤っていた（例: 上振れが一事業限定だった） |
| 既に織り込み済み | 採用時点で市場がすでに織り込んでいた |
| マクロ逆風 | Macro gate 判定の誤り、または gate が途中で反転 |
| 混雑 | 空売り残高・日々公表信用・特別注意など crowding リスク |
| 流動性不足 | 想定より出来高が伴わず entry/exit が困難 |
| ルール違反 | playbook / kill switch / position sizing 等のルール違反 |

**ルール違反トレードの扱い**: 赤ラベルで識別、playbook 改訂の input には使わない（ルール違反は playbook の不備ではなく運用の不備）。

## 7. 成功分類（初期 4 種、拡張可）

個別 review で `success_class` に記入する分類:

| 分類 | 定義 |
| --- | --- |
| 仮説的中 | research の thesis が想定通りに実現 |
| catalyst 反応 | 採用時の catalyst に市場が遅れて反応 |
| macro tailwind | マクロ追い風が想定以上に効いた |
| timing 一致 | entry のタイミング判断が正解 |

将来の retro で成功/失敗分類の再分類を行う（四半期ごと）。

## 8. Skipped trade log

- `records/04-research/` で見送り / 保留判定した銘柄を、月次 retro で追跡
- **追跡タイミング**: candidate 作成日 +15 / +30 営業日時点で、仮想 entry 価格からの騰落を 1 行追記
- **追跡方法**: 月次 retro のタイミングでまとめて実施。日次作業に乗せない
- **マクロゲート headwind で見送った候補も同様に追跡**（gate 判定の精度測定）
- skipped trade log の結果は retro 本文に集計（偽陰性率 = 見送ったが上がった銘柄の割合）

## 9. Retro での feedback ループ

### 9.1 判断基準

- サンプル数が playbook 別で 10 件未満 → **playbook 据え置きを許容**
- 代わりに checklist 差分（packet の追加チェック欄）を提案する
- 自由記述 + 反対仮説自由記述の頻出キーワードを 3-5 個抽出し、次周回で意識するポイントに落とす
- **Macro gate の判定精度** を集計（追い風判定した銘柄の +15/+30 パフォーマンス、逆風判定した不採用銘柄のパフォーマンス）
- **+15/+30 価格欠損件数** を必ず出し、tracking 未完了のまま評価しない
- **Valuation trap が 0 件だった場合**: 「サンプル不足のため valuation trap 耐性は未検証」と明記し、次周回の重点観察項目にする

### 9.2 受け渡し条件

- 次周回の運用変更点が **3 行以内で要約できる** こと
- 変更点は `playbook` / `screening 閾値` / `outlook` 運用 / `research` 選定基準 のどこに反映するかも明示

## 10. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| review 下書き生成 | ○ | |
| pnl_pct 計算 | ○ | |
| skipped trade log 追跡の騰落集計 | ○ | |
| **失敗分類の確定** | | ○ |
| **成功分類の確定** | | ○ |
| **playbook 改訂判断** | | ○ |

## 11. 参考

- [`../philosophy.md`](../philosophy.md): 思想（feedback loop 先行）
- [`../architecture.md`](../architecture.md): 全体構造
- [`trades.md`](./trades.md): source となる trades の仕様
- [`research.md`](./research.md): skipped trade log source
- [`../screening/failure-taxonomy.md`](../screening/failure-taxonomy.md): 失敗分類詳細
- [`../templates/review.md`](../templates/review.md): 個別 review template
- [`../templates/retro-monthly.md`](../templates/retro-monthly.md): 月次 retro template
