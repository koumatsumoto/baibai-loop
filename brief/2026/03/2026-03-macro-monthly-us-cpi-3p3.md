---
type: periodic
scope: japan
ai-draft: true
published_at: "2026-04-24T18:00:00+09:00"
sources:
  - "https://www.bls.gov/news.release/cpi.nr0.htm"
  - "https://www.bls.gov/news.release/empsit.nr0.htm"
  - "https://www.bea.gov/news/schedule"
  - "https://www.census.gov/retail/sales.html"
  - "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260318a.htm"
  - "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/march-2026"
  - "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/key_ecb_interest_rates/html/index.en.html"
  - "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html"
  - "https://www.stat.go.jp/data/roudou/sokuhou/tsuki/index.html"
  - "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/koyou_roudou/koyou/shokugyou/index.html"
  - "https://www.meti.go.jp/statistics/tyo/iip/result-1.html"
  - "https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260319a.pdf"
---

# Brief Japan Monthly: 2026-03 macro-monthly (us-cpi-3p3)

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（[`/docs/components/brief.md`](/docs/components/brief.md)）

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [`/docs/design-principles.md`](/docs/design-principles.md) の「事実と分析の分離」節を参照）

対象月: 2026-03
観測日: 2026-04-24
前月 brief: [../02/2026-02-macro-monthly-jp-core-cpi-sub2.md](../02/2026-02-macro-monthly-jp-core-cpi-sub2.md)
前年同月 brief: 該当なし

月次〜四半期で更新される経済統計を集約する。週次 / 日次の brief はこのファイルを参照するだけにし、月次データを再掲しない。

## 1. 世界情勢レイヤーの月次統計

### 1.1 米国

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| CPI (YoY, 総合, NSA) | +3.3% | +2.4%（2026-02） | 2026-04-10 | [BLS CPI News Release 2026 M03](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| コア CPI (YoY, 除食品・エネルギー) | +2.6% | +2.5%（2026-02） | 2026-04-10 | [BLS CPI News Release 2026 M03](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| CPI (MoM, SA, 総合) | +0.9% | +0.3%（2026-02） | 2026-04-10 | [BLS CPI News Release 2026 M03](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| 非農業部門雇用者数 (前月差, 速報) | +178千人 | -133千人（2026-02 改定） | 2026-04-03 | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.nr0.htm) (2026-04-19取得) |
| 失業率 | 4.3% | 4.4%（2026-02） | 2026-04-03 | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.nr0.htm) (2026-04-19取得) |
| コア PCE デフレーター (YoY) | 未公表（次回予定: 2026-04-30） | +3.0%（2026-02） | — | [BEA Release Schedule](https://www.bea.gov/news/schedule) (2026-04-19取得) |
| 小売売上高 (MoM, advance) | +1.7% | +0.6%（2026-02、改定 +0.7%） | 2026-04-21 | [Census Retail Sales](https://www.census.gov/retail/sales.html) (2026-04-24取得) |

### 1.2 主要中央銀行政策金利

| 指標 | 値 | 前回 | 決定日 | ソース |
|---|---|---|---|---|
| FF 目標レンジ上限 | 3.75% | 3.75%（2026-01 FOMC 据置、2月非開催） | 2026-03-18 | [Fed FOMC Statement 2026-03-18](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260318a.htm) (2026-04-19取得) |
| Bank Rate (BoE) | 3.75% | 3.75%（2026-02 MPC 据置） | 2026-03-18 | [BoE MPC Mar 2026](https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/march-2026) (2026-04-19取得) |
| Deposit Facility (ECB) | 2.00% | 2.00%（2026-02 据置） | 2026-03 | [ECB Key Rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/key_ecb_interest_rates/html/index.en.html) (2026-04-19取得) |

## 2. 日本経済レイヤーの月次統計

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| コア CPI (YoY, 全国, 除生鮮) | +1.8% | +1.6%（2026-02） | 2026-04-24 | [総務省 CPI 全国 2026年3月分](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) (2026-04-24取得) |
| コアコア CPI (YoY, 全国, 除生鮮・エネルギー) | +2.4% | +2.5%（2026-02） | 2026-04-24 | [総務省 CPI 全国 2026年3月分](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) (2026-04-24取得) |
| 完全失業率 (季調済) | 未公表（次回予定: 2026-04-28前後） | 2.6%（2026-02） | — | [総務省 労働力調査](https://www.stat.go.jp/data/roudou/sokuhou/tsuki/index.html) (2026-04-24取得) |
| 有効求人倍率 (季調済) | 未公表（次回予定: 2026-04-28前後） | 1.19倍（2026-02） | — | [厚労省 一般職業紹介状況](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/koyou_roudou/koyou/shokugyou/index.html) (2026-04-24取得) |
| 鉱工業生産指数 (MoM, 速報) | 未公表（次回予定: 2026-04-30） | -2.1%（2026-02） | — | [経産省 IIP](https://www.meti.go.jp/statistics/tyo/iip/result-1.html) (2026-04-24取得) |
| 無担保コールレート誘導目標 | 0.75% | 0.75%（2026-01 MPM 据置、2月非開催） | 2026-03-19 | [BOJ MPM 2026-03-19 PDF](https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/k260319a.pdf) (2026-04-19取得) |

## 3. 差分データ

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [`/docs/workflow.md`](/docs/workflow.md) の「差分データ」節を参照）。

### 3.1 前月比・前年比サマリ

| 指標 | 今月値 | 前月値 | MoM差 | ソース |
|---|---|---|---|---|
| 米 CPI YoY | +3.3% | +2.4% | +0.9pt | 上記 1.1 参照 |
| 米 コア CPI YoY | +2.6% | +2.5% | +0.1pt | 上記 1.1 参照 |
| 米 小売売上高 MoM | +1.7% | +0.6% | +1.1pt | 上記 1.1 参照 |
| 米 失業率 | 4.3% | 4.4% | -0.1pt | 上記 1.1 参照 |
| 日本 コア CPI YoY | +1.8% | +1.6% | +0.2pt | 上記 2 参照 |
| 日本 コアコア CPI YoY | +2.4% | +2.5% | -0.1pt | 上記 2 参照 |

### 3.2 閾値超えの変化

閾値は [`/docs/workflow.md`](/docs/workflow.md) の「差分データの閾値（月次、macro-monthly 用）」節を参照。

- 🔺 Major: 米 CPI YoY: +2.4% → +3.3% (+0.9pt) ※ CPI系 YoY 閾値 Major ±0.5pt 超え

### 3.3 方向履歴（過去4か月）

データ不足（観測対象月数: 3、過去4か月に満たない）。参考として 2026-01 → 2026-03 の推移のみ記録:

- 米 CPI YoY: `[→↑]`（2026-01 2.4 → 2026-02 2.4 → 2026-03 3.3）
- 米 コア CPI YoY: `[→↑]`（2026-01 2.5 → 2026-02 2.5 → 2026-03 2.6）
- 米 失業率: `[↑↓]`（2026-01 4.3 → 2026-02 4.4 → 2026-03 4.3）

## 4. 今月の事実メモ（数値的に顕著な点のみ）

- 米 CPI 総合 YoY が +2.4% → +3.3% に上昇（Major 閾値超え、+0.9pt）
- 米 2026-03 小売売上高は +1.7% MoM、前月 +0.6% から拡大
- 日本 2026-03 全国コア CPI は +1.8%、コアコア CPI は +2.4%（2026-04-24 公表）
- 2026-03-18 Fed FOMC: FF レンジ 3.50-3.75% 据置
- 2026-03-19 BoJ MPM: 無担保コールレート 0.75% 据置

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
