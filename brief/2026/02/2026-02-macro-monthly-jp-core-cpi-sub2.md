# Macro Monthly: 2026-02 macro-monthly (jp-core-cpi-sub2)

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [../../../docs/design-principles.md](../../../docs/design-principles.md) の「事実と分析の分離」節を参照）

対象月: 2026-02
観測日: 2026-04-19
前月 journal: [../01/2026-01-macro-monthly-overview.md](../01/2026-01-macro-monthly-overview.md)
前年同月 journal: 該当なし

## 1. 世界情勢レイヤーの月次統計

### 1.1 米国

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| CPI (YoY, 総合, NSA) | +2.4% | +2.4%（Jan） | 2026-03-11 | [BLS CPI Archive M02](https://www.bls.gov/news.release/archives/cpi_03112026.htm) (2026-04-19取得) |
| コア CPI (YoY, 除食品・エネルギー) | +2.5% | +2.5%（Jan） | 2026-03-11 | [BLS CPI News Release](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| CPI (MoM, SA, 総合) | +0.3% | +0.3%（Jan） | 2026-03-11 | [BLS CPI News Release](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| 非農業部門雇用者数 (前月差, 改定値) | -133千人 | +160千人（Jan 改定） | 2026-03-06 | [BLS TED: Feb Payrolls](https://www.bls.gov/opub/ted/2026/total-nonfarm-payroll-employment-down-by-92000-in-february-2026.htm) (2026-04-19取得) |
| 失業率 | 4.4% | 4.3%（Jan） | 2026-03-06 | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.nr0.htm) (2026-04-19取得) |
| コア PCE デフレーター (YoY) | +3.0% | +3.1%（Jan） | 2026-04-09 頃 | [BEA Personal Income & Outlays Feb 2026 PDF](https://www.bea.gov/sites/default/files/2026-04/pi0226.pdf) (2026-04-19取得) |
| 小売売上高 (MoM, advance) | +0.6% | -0.1%（Jan 改定） | 2026-04-01 | [Census MARTS](https://www.census.gov/retail/marts/www/marts_current.pdf) (2026-04-19取得) |

### 1.2 主要中央銀行政策金利

| 指標 | 値 | 前回 | 決定日 | ソース |
|---|---|---|---|---|
| FF 目標レンジ上限 | 3.75% | 3.75%（2026-01 FOMC 据置） | — (2 月 FOMC 非開催) | [Fed FOMC Calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) (2026-04-19取得) |
| Bank Rate (BoE) | 3.75% | 3.75%（2025-12 据置） | 2026-02-04 | [BoE MPC Feb 2026](https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/february-2026) (2026-04-19取得) |
| Deposit Facility (ECB) | 2.00% | 2.00%（2025-12 据置） | 2026-02-05 | [ECB MP Decision 2026-02-05](https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.mp260205~001d26959b.en.html) (2026-04-19取得) |

## 2. 日本経済レイヤーの月次統計

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| コア CPI (YoY, 全国, 除生鮮) | +1.6% | +2.0%（Jan） | 2026-03-20 頃 | [総務省 CPI 全国 (2026 年 2 月分 PDF)](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/pdf/zenkoku.pdf) (2026-04-19取得) |
| コアコア CPI (YoY, 全国, 除生鮮・エネルギー) | +2.5% | +2.6%（Jan） | 2026-03-20 頃 | [総務省 CPI](https://www.stat.go.jp/data/cpi/) (2026-04-19取得) |
| 完全失業率 (季調済) | 2.6% | 2.7%（Jan） | 2026-04-01 頃 | [総務省 労働力調査](https://www.stat.go.jp/data/roudou/sokuhou/tsuki/index.html) (2026-04-19取得) |
| 有効求人倍率 (季調済) | 1.19 倍 | 1.18（Jan） | 2026-04-01 頃 | [厚労省 一般職業紹介状況](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/koyou_roudou/koyou/shokugyou/index.html) (2026-04-19取得) |
| 鉱工業生産指数 (MoM, 速報) | -2.1% | +2.2%（Jan） | 2026-03-31 | [経産省 IIP](https://www.meti.go.jp/statistics/tyo/iip/result-1.html) (2026-04-19取得) |
| 無担保コールレート誘導目標 | 0.75% | 0.75%（2026-01 MPM 据置） | — (2 月 MPM 非開催) | [BOJ 金融政策決定会合](https://www.boj.or.jp/mopo/mpmsche_minu/index.htm) (2026-04-19取得) |

## 3. 差分データ

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データ」節を参照）。

### 3.1 前月比・前年比サマリ

YoY 系指標の MoM 変化 (今月 YoY - 前月 YoY):

| 指標 | 今月値 | 前月値 | MoM 差 | ソース |
|---|---|---|---|---|
| 米 CPI YoY | +2.4% | +2.4% | 0 pt | 上記 1.1 参照 |
| 米 コア CPI YoY | +2.5% | +2.5% | 0 pt | 上記 1.1 参照 |
| 米 失業率 | 4.4% | 4.3% | +0.1 pt | 上記 1.1 参照 |
| 米 コア PCE YoY | +3.0% | +3.1% | -0.1 pt | 上記 1.1 参照 |
| 日本 コア CPI YoY | +1.6% | +2.0% | -0.4 pt | 上記 2 参照 |
| 日本 コアコア CPI YoY | +2.5% | +2.6% | -0.1 pt | 上記 2 参照 |
| 日本 完全失業率 | 2.6% | 2.7% | -0.1 pt | 上記 2 参照 |

### 3.2 閾値超えの変化

閾値は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データの閾値（月次、macro-monthly 用）」節を参照。

- 🔸 Notable: 日本 コア CPI YoY: +2.0% → +1.6% (-0.4 pt) ※CPI 系 YoY 閾値 Notable ±0.3 pt

### 3.3 方向履歴（過去 4 か月）

データ不足（観測対象月数: 2、過去 4 か月に満たない）。

## 4. 今月の事実メモ（数値的に顕著な点のみ）

- 日本コア CPI YoY が 2.0% → 1.6% に低下（Notable 閾値超え）
- BoE 2 月 MPC: Bank Rate 3.75% 据置、投票 5-4 分裂
- ECB 2 月理事会: 預金ファシリティ金利 2.00% 据置
- 米 非農業部門雇用者数 2 月改定 -133 千人（速報 -92 千人）

---

記入ルールは [../../../docs/workflow.md](../../../docs/workflow.md) を、設計根拠は [../../../docs/design-principles.md](../../../docs/design-principles.md) を参照。
