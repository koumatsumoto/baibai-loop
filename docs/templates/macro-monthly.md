# Macro Monthly: YYYY-MM {kind} ({slug})

対象月: YYYY-MM
観測日: YYYY-MM-DD

月次〜四半期で更新される経済統計を集約する。週次 / 日次の journal (`world-weekly` 等) はこのファイルを参照するだけにし、月次データを再掲しない。

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

## 3. 今月の特徴点（事実記述のみ）

- [前月比・前年比で特徴のある変動、発表と市場反応の事実のみ。因果や予測は書かない]

---

記入ルールは [../workflow.md](../workflow.md)、設計根拠は [../design-principles.md](../design-principles.md) を参照。
