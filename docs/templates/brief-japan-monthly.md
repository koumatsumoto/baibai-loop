---
type: periodic
scope: japan
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
sources:
  - "URL1"
  - "URL2"
---

# Brief Japan Monthly: YYYY-MM {kind} ({slug})

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`../components/brief.md`](../components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`../design-principles.md`](../design-principles.md) の「事実と分析の分離」節を参照）

対象月: YYYY-MM
観測日: YYYY-MM-DD
前月 brief: [YYYY-MM-macro-monthly-{slug}.md](../MM/YYYY-MM-macro-monthly-{slug}.md)（初回の場合は「該当なし（差分データ初回）」）
前年同月 brief: [YYYY-MM-macro-monthly-{slug}.md](../../YYYY/MM/YYYY-MM-macro-monthly-{slug}.md)（存在しない場合は「該当なし」）

月次〜四半期で更新される経済統計を集約する。週次 / 日次の brief (`world-weekly` 等) はこのファイルを参照するだけにし、月次データを再掲しない。

## 1. 世界情勢レイヤーの月次統計

### 1.1 米国

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| CPI (YoY, 総合) | X.X% | X.X% | YYYY-MM-DD | [BLS](URL) (YYYY-MM-DD取得) |
| コア CPI (YoY, 除食品・エネルギー) | X.X% | X.X% | YYYY-MM-DD | [BLS](URL) (YYYY-MM-DD取得) |
| 雇用統計 非農業部門雇用者数 (前月比) | +XX万 | +XX万 | YYYY-MM-DD | [BLS](URL) (YYYY-MM-DD取得) |
| 失業率 | X.X% | X.X% | YYYY-MM-DD | [BLS](URL) (YYYY-MM-DD取得) |
| PCE デフレーター (YoY, コア) | X.X% | X.X% | YYYY-MM-DD | [BEA](URL) (YYYY-MM-DD取得) |
| 小売売上高 (MoM) | +X.X% | +X.X% | YYYY-MM-DD | [Census](URL) (YYYY-MM-DD取得) |

### 1.2 主要中央銀行政策金利

| 指標 | 値 | 前回 | 決定日 | ソース |
|---|---|---|---|---|
| 政策金利 (FF 目標レンジ上限) | 米国 | X.XX% | X.XX% | YYYY-MM-DD | [Fed](URL) (YYYY-MM-DD取得) |
| 政策金利 (Bank Rate) | 英国 | X.XX% | X.XX% | YYYY-MM-DD | [BoE](URL) (YYYY-MM-DD取得) |
| 政策金利 (Deposit Facility) | 欧州 | X.XX% | X.XX% | YYYY-MM-DD | [ECB](URL) (YYYY-MM-DD取得) |

## 2. 日本経済レイヤーの月次統計

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| コア CPI (YoY, 全国, 除生鮮) | X.X% | X.X% | YYYY-MM-DD | [総務省](URL) (YYYY-MM-DD取得) |
| コアコア CPI (YoY, 全国, 除生鮮・エネルギー) | X.X% | X.X% | YYYY-MM-DD | [総務省](URL) (YYYY-MM-DD取得) |
| 完全失業率 | X.X% | X.X% | YYYY-MM-DD | [総務省](URL) (YYYY-MM-DD取得) |
| 有効求人倍率 | X.XX | X.XX | YYYY-MM-DD | [厚労省](URL) (YYYY-MM-DD取得) |
| 鉱工業生産指数 (MoM) | +X.X% | +X.X% | YYYY-MM-DD | [経産省](URL) (YYYY-MM-DD取得) |
| 無担保コールレート誘導目標 | X.XX% | X.XX% | YYYY-MM-DD | [日銀](URL) (YYYY-MM-DD取得) |

## 3. 差分データ

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [../workflow.md](../workflow.md) の「差分データ」節を参照）。

### 3.1 前月比・前年比サマリ

| 指標 | 今月値 | 前月比 (MoM) | 前年比 (YoY) | ソース |
|---|---|---|---|---|
| [指標名] | X.X% | +X.X pt | +X.X pt | [ソース](URL) (YYYY-MM-DD取得) |

### 3.2 閾値超えの変化

閾値は [../workflow.md](../workflow.md) の「差分データの閾値（月次、macro-monthly 用）」節を参照。

- 🔺 Major: [指標]: [前月値] → [今月値] ([変化量])
- 🔸 Notable: [指標]: [前月値] → [今月値] ([変化量])

閾値超えがない場合は「該当なし」と記載する。

### 3.3 方向履歴（過去 4 か月）

- [指標]: `[↑↑↑↑]`（直近月が右端）
- 方向反転があれば機械的に記録

過去 4 か月分のデータが不足する場合は「データ不足（観測対象月数: N）」と記載する。

## 4. 今月の事実メモ（数値的に顕著な点のみ）

- [前月比・前年比で閾値を超えた事実、あるいは発表時の市場反応の事実のみ。因果・予測・解釈は書かない]

---

記入ルールは [../workflow.md](../workflow.md) を、設計根拠は [../design-principles.md](../design-principles.md) を参照。
