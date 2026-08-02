---
title: "株数減少・予想増配の長期production判断"
summary: "株数減少は3y control、予想増配は独立3yと5yで条件を外したため、両成分ともproductionへ採用しない。"
doc_type: report
status: active
date: 2026-08-02
---

# 株数減少・予想増配の長期production判断

価値tier: T1 — 低valuation候補のバリュートラップ率を下げる還元変化について、長期・authority・一次IRの事前登録条件を満たさない成分を判断面へ接続しない。

## 結論

- **H-S `share_count_reduction_streak`: `negative`**。独立3y confirmと固定5yの方向は強く、一次IR固定標本も12社すべてclassifyできた。しかし固定3yの`dividend_yield` controlでtrap rateが+1.12pt悪化し、事前登録した全control条件を外した。
- **H-D `dps_guidance_up`: `negative`**。独立3y confirmのmedian deltaは-9.52pt、固定5yは-17.03ptで、全6 controlも同じ逆方向だった。

加えて、独立3y confirmの2 cohortはpriced-master対象forward returnの解決が不完全で、production authorityがblockedだった。方向・controlのfailがあるため最終判定は`insufficient`ではなく`negative`とするが、blocked authorityを良い固定長期結果で上書きもしない。

candidate annotation、UI、warning、gate、ranking、FV、E[r]、sizingは変更しない。production配線の別issueも作らない。`share_count_reduction_streak`を「自社株買い」と表示する根拠にも使わない。

## 固定scopeとartifact

事前登録はcommit `e6ca851`、結果を読む前に固定した単発診断実装はcommit `70aee43`である。入力、cohort、control、一次IR標本、採否条件は [`2026-08-02-share-return-components-production-preregistration.md`](./2026-08-02-share-return-components-production-preregistration.md) を正本とする。

- 独立3y confirm: `2023-06-30`、`2023-07-31`
- 固定長期: `2020-01-31`、`2020-05-29`、`2021-01-29`、`2021-05-31` × `3y` / `5y`
- metric: resolvedな流動性母集団のmedian price returnに対する`price_return_only` excess
- trap: `excess < -0.20`
- 独立3y component artifact SHA-256: `d8d2e50d80feeb45a75634b15b1c93bc66adf731a2fff973add9d3a966c4832d`
- 固定3y/5y component artifact SHA-256: `d97e63b791c631e24ecf1d3704fd44b13284658f5cf98af85b32c9df672a2986`
- 独立3y authority artifact SHA-256: `b677141a146eb6873a186c5cee656ca4e64df1b06693ba010797f46ba5a8694a`
- 固定3y/5y authority artifact SHA-256: `fc3255b383ca1832438084973ddbf13d492e9df2261e4bdc79aecc3b8116101c`

固定3y/5y authorityはrequired 8組すべてeligible、blocking reasonなしだった。独立3y authorityは、3yだけのrun全体に対する`missing_required_horizons:5y`とは別に、両cohort固有の`priced_master_without_universe_return_unresolved`を報告した。

| as-of | priced-master対象 | resolved target | integrity |
| --- | ---: | ---: | --- |
| 2023-06-30 | 16 | 15 | blocked |
| 2023-07-31 | 10 | 8 | blocked |

## H-S: グロス発行済株式数減少

### 主結果

| window | true / false n | mean median delta | positive cohort share | mean trap delta | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 独立3y confirm | 55 / 378 | **+8.24pt** | 100% | **-12.15pt** | effect pass、authority blocked |
| 固定3y | 72 / 638 | **+30.67pt** | 100% | **-5.37pt** | control fail |
| 固定5y | 68 / 610 | **+57.98pt** | 100% | **-6.01pt** | pass |

独立3yは`2023-06-30`が+10.78pt / trap -10.17pt、`2023-07-31`が+5.69pt / -14.13ptだった。全6 aggregate controlはmedian正・trap非正で、必須の`price_change_60d`も+9.92pt / -12.67ptだった。

固定長期のcontrolは次のとおり。固定3yの`dividend_yield`だけtrap非悪化条件を外した。良いraw delta、5y、momentum controlでこのfailを上書きしない。

| control | 3y median / trap delta | 5y median / trap delta |
| --- | ---: | ---: |
| `dividend_yield` | +18.84pt / **+1.12pt** | +73.43pt / -0.30pt |
| `per_trailing` | +12.67pt / -4.15pt | +28.52pt / -3.44pt |
| `pbr` | +14.72pt / -1.56pt | +65.76pt / -0.84pt |
| `market_cap_oku` | +18.59pt / -2.19pt | +52.06pt / -4.76pt |
| `avg_turnover_oku` | +28.02pt / -4.82pt | +54.64pt / -6.30pt |
| `price_change_60d` | +6.38pt / -2.71pt | +32.47pt / -3.05pt |

### 一次IR固定標本

`2025-06-30` panelで固定した12社について、最新側のFY-to-FY gross shares減少を会社IR、法定開示、取引所開示で照合した。priorは株式分割を最新側基準へ調整している。

| ticker / company | adjusted gross shares | 一次資料とattribution | class |
| --- | ---: | --- | ---: |
| 8395 佐賀銀行 | 17,135,909 → 16,935,909 | [2025-02-07会社IR](https://www.sagabank.co.jp/news/info/files/info_20250207.pdf): 2月21日に200,000株を消却。標本資料では取得との対応を確定できない | 2 |
| 5741 UACJ | 48,328,193 → 46,328,193 | [2025-05-30株主総会資料](https://www.uacj.co.jp/ir/library/pdf/2025/shoshu_20250620.pdf): 2月12日同一取締役会決議で3,000,000株を取得し、3月14日に2,000,000株を消却 | 1 |
| 5186 ニッタ | 30,272,503 → 29,272,503 | [2025-02-07取引所開示](https://www2.jpx.co.jp/disc/51860/140120250206564535.pdf): 同一取締役会決議で300,000株取得、1,000,000株消却。消却全量との対応は確定できない | 2 |
| 9107 川崎汽船 | 714,728,067 → 639,172,067 | [2025-06-19会社掲載法定開示](https://www.kline.co.jp/ja/news/ir/auto_20250619100400_S100VYMJ/pdfFile.pdf): 39,556,000株と36,000,000株をそれぞれ取得後に全量消却 | 1 |
| 6326 クボタ | 1,176,666,846 → 1,150,896,846 | [2024-12-16会社IR](https://www.kubota.co.jp/ir/news-support/news/data/nws20241216.pdf)、[株主総会資料](https://www.kubota.co.jp/ir/stock/meeting/data/cn135-1.pdf): 25,771,700株を取得後、12月27日に25,770,000株を消却 | 1 |
| 8377 ほくほくFG | 125,370,814 → 123,458,714 | [2024-07-10取得完了](https://www.hokuhoku-fg.co.jp/news/docs/20240710_stock.pdf)、[2024-07-25消却決議](https://www.hokuhoku-fg.co.jp/news/docs/a158ac0d81db00bacef58f35c72fbab297b9c833.pdf): 1,912,100株を取得し、同数を9月30日に消却 | 1 |
| 3443 川田テクノロジーズ | 17,784,210 → 17,474,210 | [2025-06-24有価証券報告書](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100W2OA.pdf): 分割調整後に新株予約権行使+90,000株と消却-400,000株が同居し、proxyは純額-310,000株を記録 | 3 |
| 7270 SUBARU | 753,901,573 → 733,057,473 | [2024-10-11会社IR](https://www.subaru.co.jp/news/2024_10_11_125426/): 20,844,100株を取得し、その全量を10月11日に消却 | 1 |
| 7246 プレス工業 | 106,823,470 → 100,000,000 | [2025-06-25有価証券報告書](https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100W4F1.pdf): 取得と対応する2,261,000株に加え、対応未確定の4,562,470株を消却。FY減少全量は取得と結べない | 2 |
| 4202 ダイセル | 286,942,682 → 276,942,682 | [2025-06-18有価証券報告書](https://www.daicel.com/ir/pdf/yuho/yuho159.pdf): 2024年5月20日に10,000,000株を消却。標本資料ではその取得との対応を確定できない | 2 |
| 7202 いすゞ自動車 | 777,442,069 → 713,526,569 | [2025-06-25有価証券報告書](https://www.isuzu.co.jp/company/investor/financial/securities/assets/pdf/report202503.pdf): 26,568,600株、37,346,900株を各取得プログラム後に同数消却 | 1 |
| 1885 東亜建設工業 | 89,978,516 → 87,978,516 | [2024-05-13取引所開示](https://www2.jpx.co.jp/disc/18850/140120240513592841.pdf): 1:4分割後の2024年4月2日に2,000,000株を消却。標本資料では取得との対応を確定できない | 2 |

classifiableは12/12、class 1+2は11/12（計算: `11 / 12 × 100 = 91.7%`）、unresolvedは0だったため、一次IR追加条件はpassした。ただし、これはproxyが「自社株買い」を直接観測することを意味しない。自己株取得はgross sharesを減らさず、消却時だけ減る。川田テクノロジーズでは新株発行と消却の純額になり、ニッタ、プレス工業などでは同期間の取得数と消却数が一致しない。表示するとしても意味は`発行済株式数減少`までだったが、長期control failにより表示自体を採用しない。

## H-D: 予想DPS増

| window | true / false n | mean median delta | positive cohort share | mean trap delta | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 独立3y confirm | 203 / 215 | **-9.52pt** | 0% | +0.06pt | fail、authority blocked |
| 固定3y | 205 / 410 | +4.18pt | 75% | -2.07pt | pass |
| 固定5y | 201 / 389 | **-17.03pt** | 25% | -4.48pt | fail |

独立3yは2 cohortとも逆方向（-8.33pt、-10.72pt）で、全6 controlもmedianが負だった。必須の`price_change_60d`は-7.66pt / trap +1.14ptである。固定5yも全6 controlが負で、`price_change_60d`は-19.06pt / -4.50ptだった。

親評価で見えた1y design / confirmと3y designの正方向は、独立3y confirmと5yに再現しない。最新実績DPSより最新予想DPSが高いというbooleanを、持続的増配、実現増配、total shareholder returnの予測力とは読まない。

## 独立検算

`2023-06-30`の3y H-Sを、診断toolをimportせずpanel / forward CSVから標準CSV readerと中央値計算だけで再計算した。

- resolved population 1,387、population median return 26.4944%、低PER帯244、streak観測226
- true 30: median excess 49.6955%、trap 6.67%
- false 196: median excess 38.9115%、trap 16.84%
- raw median delta +10.7840pt、trap delta -10.17pt
- `price_change_60d` split 7.7610%、matched weight 30、stratified median delta +14.9526pt、trap delta -10.5363pt

artifactの+14.9527pt / -10.5340ptとの差は、toolがstratumごとのgroup rateを4桁へ丸めてから重み付けするためであり、丸め前の方向、標本、境界は一致した。

## 採否表

| 条件 | H-S | H-D |
| --- | --- | --- |
| 独立3y authority | blocked | blocked |
| 固定3y/5y authority 8組 | pass | pass |
| 独立3y effect / control / sample | pass | **effect・control fail** |
| 固定3y effect / control / sample | **dividend yield trap fail** | pass |
| 固定5y effect / control / sample | pass | **effect・全control fail** |
| 一次IR追加条件 | pass | 対象外 |
| 最終判定 | **negative** | **negative** |

forward窓は重複するため、cohort数を独立標本や有意性として扱わない。結果はprice-onlyの関連であり、還元施策が株価を上げたという因果推定でもない。

## 再現と実装境界

結果を読む前にcommit `70aee43`へ固定した単発toolで次を実行した。

```bash
.venv/bin/python tools/evaluate_return_change_components.py \
  --asof 2023-06-30 --asof 2023-07-31 \
  --horizon 3y \
  --out /tmp/share-return-components-3y-confirm.yaml

.venv/bin/python tools/evaluate_return_change_components.py \
  --asof 2020-01-31 --asof 2020-05-29 \
  --asof 2021-01-29 --asof 2021-05-31 \
  --horizon 3y --horizon 5y \
  --out /tmp/share-return-components-long.yaml
```

authority artifactは同じrequired as-ofについて`baibai-engine screening calibration-evaluate --run-purpose production_decision`で生成した。両仮説が`negative`となり反復運用しないため、単発toolとfixtureは計測commitに監査可能な形で残し、通常treeから除去した。stable CLI、schema、candidate surfaceは増やさない。
