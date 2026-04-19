# Macro Monthly: 2026-01 macro-monthly (overview)

**レイヤー**: 事実レイヤー（観測値・一次統計引用・機械的計算のみ。解釈・予測・相場観は書かない。詳細は [../../../docs/design-principles.md](../../../docs/design-principles.md) の「事実と分析の分離」節を参照）

対象月: 2026-01
観測日: 2026-04-19
前月 journal: 該当なし（差分データ初回）
前年同月 journal: 該当なし

月次〜四半期で更新される経済統計を集約する。週次 / 日次の journal はこのファイルを参照するだけにし、月次データを再掲しない。

## 1. 世界情勢レイヤーの月次統計

### 1.1 米国

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| CPI (YoY, 総合, NSA) | +2.4% | 未取得（Dec 2025） | 2026-02-11 | [BLS TED: CPI up 2.4% YoE Jan 2026](https://www.bls.gov/opub/ted/2026/consumer-prices-up-2-4-percent-over-the-year-ended-january-2026.htm) (2026-04-19取得) |
| コア CPI (YoY, 除食品・エネルギー) | +2.5% | 未取得 | 2026-02-11 | [BLS CPI News Release](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| CPI (MoM, SA, 総合) | +0.3% | 未取得 | 2026-02-11 | [BLS CPI News Release](https://www.bls.gov/news.release/cpi.nr0.htm) (2026-04-19取得) |
| 非農業部門雇用者数 (前月差, 改定値) | +160千人 | 未取得 | 2026-02-11 | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.nr0.htm) (2026-04-19取得) |
| 失業率 | 4.3% | 未取得 | 2026-02-11 | [BLS Employment Situation](https://www.bls.gov/news.release/empsit.nr0.htm) (2026-04-19取得) |
| コア PCE デフレーター (YoY) | +3.1% | 未取得 | 2026-03-13 | [BEA Personal Income & Outlays Jan 2026](https://www.bea.gov/news/2026/personal-income-and-outlays-january-2026) (2026-04-19取得) |
| 小売売上高 (MoM, advance, 改定値) | -0.1% | 未取得（速報 -0.2%） | 2026-02-14 | [Census MARTS](https://www.census.gov/retail/marts/www/marts_current.pdf) (2026-04-19取得) |

### 1.2 主要中央銀行政策金利

| 指標 | 値 | 前回 | 決定日 | ソース |
|---|---|---|---|---|
| FF 目標レンジ上限 | 3.75% | 3.75%（2025-12 FOMC 据置） | 2026-01-28 | [Fed FOMC Statement 2026-01-28](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260128a.htm) (2026-04-19取得) |
| Bank Rate (BoE) | 3.75% | 3.75%（2025-12 MPC 継続、1 月 MPC なし） | — | [BoE Bank Rate](https://www.bankofengland.co.uk/monetary-policy) (2026-04-19取得) |
| Deposit Facility (ECB) | 2.00% | 2.00%（2025-12 理事会決定継続、1 月理事会なし） | — | [ECB Key Rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/key_ecb_interest_rates/html/index.en.html) (2026-04-19取得) |

## 2. 日本経済レイヤーの月次統計

| 指標 | 値 | 前回 | 発表日 | ソース |
|---|---|---|---|---|
| コア CPI (YoY, 全国, 除生鮮) | +2.0% | 未取得（Dec 2025） | 2026-02-20 | [総務省 CPI 全国最新](https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html) (2026-04-19取得) |
| コアコア CPI (YoY, 全国, 除生鮮・エネルギー) | +2.6% | 未取得 | 2026-02-20 | [総務省 CPI](https://www.stat.go.jp/data/cpi/) (2026-04-19取得) |
| 完全失業率 (季調済) | 2.7% | 未取得 | 2026-03-04 | [総務省 労働力調査](https://www.stat.go.jp/data/roudou/sokuhou/tsuki/index.html) (2026-04-19取得) |
| 有効求人倍率 (季調済) | 1.18 倍 | 未取得 | 2026-03-04 | [厚労省 一般職業紹介状況](https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/koyou_roudou/koyou/shokugyou/index.html) (2026-04-19取得) |
| 鉱工業生産指数 (MoM, 速報) | +2.2% | 未取得 | 2026-02-28 | [経産省 IIP](https://www.meti.go.jp/statistics/tyo/iip/result-1.html) (2026-04-19取得) |
| 無担保コールレート誘導目標 | 0.75% | 0.75%（2025-12 MPM 据置） | 2026-01-23 | [BOJ 総裁記者会見 2026-01-26](https://www.boj.or.jp/about/press/kaiken_2026/kk260126a.pdf) (2026-04-19取得) |

## 3. 差分データ

この節は計算結果とルール適用の結果のみを記録する。解釈・予測・相場観は書かない（詳細は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データ」節を参照）。

### 3.1 前月比・前年比サマリ

前月データ未取得のため、MoM 差分は計算不能。YoY 値は各指標の欄に記載。前月データは次月以降の journal 作成時に補完する。

### 3.2 閾値超えの変化

閾値は [../../../docs/workflow.md](../../../docs/workflow.md) の「差分データの閾値（月次、macro-monthly 用）」節を参照。

- 政策金利: Fed / BoJ いずれも据置。変更なし (閾値判定は「変更あり (±25 bp)」基準で該当せず)

CPI / 雇用関連は前月データ未取得のため閾値判定不能。

### 3.3 方向履歴（過去 4 か月）

データ不足（観測対象月数: 1、差分データ初回のため履歴構築不可）。

## 4. 今月の事実メモ（数値的に顕著な点のみ）

- 2026-01-28 FOMC: FF レンジ 3.50-3.75% を据置
- 2026-01-23 BoJ MPM: 無担保コールレート 0.75% を据置
- 日本コア CPI YoY +2.0% は日銀物価安定目標 (2%) の水準

---

記入ルールは [../../../docs/workflow.md](../../../docs/workflow.md) を、設計根拠は [../../../docs/design-principles.md](../../../docs/design-principles.md) を参照。
