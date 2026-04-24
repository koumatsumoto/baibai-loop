# components/screened.md

Baibai-Loop 4 成分アーキテクチャの **(b) スクリーニング通過銘柄** の運用仕様。狭義のスクリーニング = 機械的ふるいの完了形を指す。全体構造は [`../architecture-v1.md`](../architecture-v1.md)、スクリーニング詳細は [`../screening/`](../screening/) 配下を参照。

## 1. 役割

- universe（日本株普通株、時価総額 300 億円以上、20 営業日平均売買代金 2 億円以上）に対し、valuation 指標でふるいをかけ、**通過銘柄 list を事実として記録**
- 事実層のため解釈は入れない（反対仮説・原因仮説は research 側で行う）
- Micro track の出発点として、`research/` の選定入力となる

## 2. 頻度

- **定期**: 週次 1 回（初期値、retro で調整）
- 実行タイミング: 週初 / 週末の決まった曜日（例: 毎週月曜朝 or 金曜夕方）

## 3. Path と命名

```
screened/YYYY/MM/YYYY-MM-DD.md
```

1 実行 = 1 ファイル（週次運用のため）。

## 4. Front matter 必須項目

```yaml
---
run_date: "YYYY-MM-DD"              # 実行日
universe_size: 整数                 # その時点の universe 銘柄数
filters:                            # 適用した閾値・条件
  min_market_cap_oku: 300
  min_avg_turnover_oku: 2
  # その他閾値
tickers:                            # 通過銘柄 list
  - ticker: "7203"
    name: "..."
    per_forward: 8.2 | null
    per_trailing: 9.5
    pbr: 0.72
    ev_ebitda: 4.8
    p_s: 0.6
    pcfr: 5.1
    sector_33: "輸送用機器"         # 東証 33 業種
    threshold_hit:                  # どの閾値条件を満たして通過したか（OR 条件）
      - sector_median_under_20pct
      - self_range_bottom_20pct
---
```

- ticker は文字列として quote 必須（先頭 0 落ち防止）
- 欠損値（例: forward EPS 未公表）は明示的に `null`
- `threshold_hit`: mechanical-v1 の閾値条件 3 種のどれを満たしたか（OR 条件、複数 hit 可）

## 5. ワークフロー

### 5.1 実行手順

1. 最新 universe を取得（J-Quants core + JPX 除外条件適用）
2. 各 ticker の valuation 指標を算出（[`../screening/valuation-metrics.md`](../screening/valuation-metrics.md) 参照）
3. 閾値条件（[`../screening/mechanical-v1.md`](../screening/mechanical-v1.md) の 3 種 OR）を適用
4. 通過銘柄を `tickers` 配列として front matter に記録
5. 本文には補足情報（実行時の市場環境メモ、除外した特殊ケース等）を事実として記録

### 5.2 実装

- 初期は **手動 + AI 下書き**（J-Quants から数値取得 → 手動で閾値適用 → front matter に記入）
- 将来 script 化を検討（v1 運用で手間を計測してから）

## 6. research への接続

- `research/` の front matter `screened_ref` で本ファイルを参照
- 選定プロセス: 最新 `screened/` と最新 `view/` を突き合わせ、`view` で tailwind/neutral の業種/地域の ticker を候補に残す（headwind 除外）
- 詳細: [`research.md`](./research.md) の選定プロセス

## 7. 事実と分析の分離

- screened は **事実層**。valuation 数値・閾値 hit 判定は機械的
- 「この銘柄は割安だ」という解釈は research 側で行う
- 「通過した」ことは事実だが、「採用すべき」は解釈

## 8. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| valuation 指標の算出 | ○ | 異常値の手動確認 |
| 閾値適用・threshold_hit 判定 | ○ | |
| front matter 整備 | ○ | |
| 数値ソースの一次確認 | ○ | 最終責任 |
| 最終 commit | | ○ |

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想（事実と分析の分離、マクロ優位）
- [`../architecture-v1.md`](../architecture-v1.md): 全体構造
- [`../screening/`](../screening/): スクリーニングサブシステム詳細
- [`../screening/universe-rules.md`](../screening/universe-rules.md): universe 境界条件
- [`../screening/valuation-metrics.md`](../screening/valuation-metrics.md): 指標算出仕様
- [`../screening/mechanical-v1.md`](../screening/mechanical-v1.md): 機械的ふるい仕様（閾値 3 種 OR）
- [`../templates/screened.md`](../templates/screened.md): template
