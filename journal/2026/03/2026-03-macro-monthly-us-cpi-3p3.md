# Macro Monthly: 2026-03 macro-monthly (us-cpi-3p3)

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [../../../docs/design-principles.md](../../../docs/design-principles.md) の「事実と分析の分離」節を参照）

対象月: 2026-03
観測日: 2026-04-19
前月 journal: [../02/2026-02-macro-monthly-jp-core-cpi-sub2.md](../02/2026-02-macro-monthly-jp-core-cpi-sub2.md)
前年同月 journal: 該当なし

## 1. 世界情勢レイヤーの月次統計

### 1.1 米国

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| CPI (YoY, 総合, NSA) | +3.3% | +2.4%（Feb） | 2026-04-10 | [BLS CPI News Release 2026 M03](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| コア CPI (YoY, 除食品・エネルギー) | +2.6% | +2.5%（Feb） | 2026-04-10 | [BLS CPI News Release 2026 M03](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| CPI (MoM, SA, 総合) | 未取得（コア MoM は +0.2%） | +0.3%（Feb） | 2026-04-10 | [BLS CPI News Release 2026 M03](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| 非農業部門雇用者数 (前月差, 速報) | +178千人 | -133千人（Feb 改定） | 2026-04-03 | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.nr0.htm) (2026-04-19取得) |
| 失業率 | 4.3% | 4.4%（Feb） | 2026-04-03 | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.nr0.htm) (2026-04-19取得) |
| コア PCE デフレーター (YoY) | 未公表（次回予定: 2026-04-30） | +3.0%（Feb） | — | [BEA Release Schedule](https://www.bea.gov/news/schedule) (2026-04-19取得) |
| 小売売上高 (MoM, advance) | 未公表（次回予定: 2026-04-21、従来 04-16 から延期） | +0.6%（Feb） | — | [Census MARTS](https://www.census.gov/retail/marts/www/marts_current.pdf) (2026-04-19取得) |

### 1.2 主要中央銀行政策金利

| 指標 | 値 | 前回 | 決定日 | ソース |
|---|---|---|---|---|
| FF 目標レンジ上限 | 3.75% | 3.75%（2026-01 FOMC 据置、2 月非開催） | 2026-03-18 | [Fed FOMC Statement 2026-03-18](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260318a.htm) (2026-04-19取得) |
| Bank Rate (BoE) | 3.75% | 3.75%（2026-02 MPC 据置） | 2026-03-18 | [BoE MPC Mar 2026](https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/march-2026) (2026-04-19取得) |
| Deposit Facility (ECB) | 2.00% | 2.00%（2026-02 据置） | 2026-03 月理事会 | [ECB Key Rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/key_ecb_interest_rates/html/index.en.html) (2026-04-19取得) |

## 2. 日本経済レイヤーの月次統計

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| コア CPI (YoY, 全国, 除生鮮) | 未公表（次回予定: 2026-04-24 または -25） | +1.6%（Feb） | — | [総務省 CPI 全国最新](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) (2026-04-19取得) |
| コアコア CPI (YoY, 全国, 除生鮮・エネルギー) | 未公表（次回予定: 2026-04-24 または -25） | +2.5%（Feb） | — | [総務省 CPI](https://www.stat.go.jp/data/cpi/) (2026-04-19取得) |
| 完全失業率 (季調済) | 未公表（次回予定: 2026-04-28 前後） | 2.6%（Feb） | — | [総務省 労働力調査](https://www.stat.go.jp/data/roudou/sokuhou/tsuki/index.html) (2026-04-19取得) |
| 有効求人倍率 (季調済) | 未公表（次回予定: 2026-04-28 前後） | 1.19（Feb） | — | [厚労省 一般職業紹介状況](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/koyou_roudou/koyou/shokugyou/index.html) (2026-04-19取得) |
| 鉱工業生産指数 (MoM, 速報) | 未公表（次回予定: 2026-04-30） | -2.1%（Feb） | — | [経産省 IIP](https://www.meti.go.jp/statistics/tyo/iip/result-1.html) (2026-04-19取得) |
| 無担保コールレート誘導目標 | 0.75% | 0.75%（2026-01 MPM 据置、2 月非開催） | 2026-03-19 | [BOJ MPM 2026-03-19 PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260319a.pdf) (2026-04-19取得) |

## 3. 差分データ

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データ」節を参照）。

### 3.1 前月比・前年比サマリ

YoY 系指標の MoM 変化 (今月 YoY - 前月 YoY):

| 指標 | 今月値 | 前月値 | MoM 差 | ソース |
|---|---|---|---|---|
| 米 CPI YoY | +3.3% | +2.4% | +0.9 pt | 上記 1.1 参照 |
| 米 コア CPI YoY | +2.6% | +2.5% | +0.1 pt | 上記 1.1 参照 |
| 米 失業率 | 4.3% | 4.4% | -0.1 pt | 上記 1.1 参照 |
| 日本 諸指標 | 未公表 | — | 計算不能 | 上記 2 参照 |

### 3.2 閾値超えの変化

閾値は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データの閾値（月次、macro-monthly 用）」節を参照。

- 🔺 Major: 米 CPI YoY: +2.4% → +3.3% (+0.9 pt) ※CPI 系 YoY 閾値 Major ±0.5 pt 超え

### 3.3 方向履歴（過去 4 か月）

データ不足（観測対象月数: 3、過去 4 か月に満たない）。参考として Jan→Feb→Mar の推移のみ記録:

- 米 CPI YoY: `[→↑]` (Jan 2.4 → Feb 2.4 → Mar 3.3)
- 米 コア CPI YoY: `[→↑]` (Jan 2.5 → Feb 2.5 → Mar 2.6)
- 米 失業率: `[↑↓]` (Jan 4.3 → Feb 4.4 → Mar 4.3)

## 4. 今月の事実メモ（数値的に顕著な点のみ）

- 米 CPI 総合 YoY が +2.4% → +3.3% に上昇（Major 閾値超え、+0.9 pt）
  - BLS リリースでは「energy +10.9%, gasoline +21.2% がヘッドラインの大半を占めた」と記載
- 2026-03-18 Fed FOMC: FF レンジ 3.50-3.75% 据置
- 2026-03-18 BoE MPC: Bank Rate 3.75% 据置、全会一致
- 2026-03-19 BoJ MPM: 無担保コールレート 0.75% 据置
- 日本のコア CPI・雇用・鉱工業生産 3 月分はすべて 4 月下旬〜月末公表予定で、2026-04-19 観測日時点では未公表

---

記入ルールは [../../../docs/workflow.md](../../../docs/workflow.md) を、設計根拠は [../../../docs/design-principles.md](../../../docs/design-principles.md) を参照。
