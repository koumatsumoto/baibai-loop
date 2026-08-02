---
title: "空売り残/ADVの長期production判断"
summary: "固定3y/5y authorityは通過したが、独立3y confirmのintegrity不足によりproduction採用を見送る。"
doc_type: report
status: active
date: 2026-08-02
---

# 空売り残/ADVの長期production判断

価値tier: T1 — 高い空売り残/ADVと将来劣後の関連を一次research前に示し、バリュートラップへ調査枠を使う確率を下げる。

## 結論

`margin_short_to_adv` のproduction判断は **`insufficient`** とする。独立3y confirmの軸、trap、control、標本は事前登録条件を満たしたが、priced-master対象のforward returnが`2023-06-30`で16件中15件、`2023-07-31`で10件中8件しか解決せず、両cohortのintegrityがblockedだった。方向または効果量のnegativeではなく、authority不足である。

事前登録どおりcandidate、UI、warning、gate、ranking、FV、E[r]、sizingを変更せず、production配線の別issueも作らない。固定cohortを差し替えず、forward returnを完全に解決できた場合だけ同じ条件で再判定する。

この結果は、空売り主体、借株コスト、貸株料、取引理由を観測した因果推定ではない。高い空売り残/ADVとprice-only excessの劣後が固定窓で関連した、という限定された結論である。

## 固定scopeとartifact

事前登録はcommit `0e37eb3`、結果を読む前に固定したoptional metric authorityとsurvivorship判定はcommit `32e00ed` である。入力、方向、cohort、control、採否条件は [`2026-08-02-margin-short-production-preregistration.md`](./2026-08-02-margin-short-production-preregistration.md) を正本とする。

- 独立3y confirm: `2023-06-30`、`2023-07-31`
- 固定production authority: `2020-01-31`、`2020-05-29`、`2021-01-29`、`2021-05-31` × `3y` / `5y`
- metric basis: `price_return_only`
- cache schema: `10`
- 独立3y confirm SHA-256: `8d8ad6bbb7e0ee761375924e9e989ad2001f2487cdaea00f76071e5e6f34e6eb`
- 独立3y integrity audit SHA-256: `0a48d8981ec15a52f1ce4ca99d0ad0c21cc460122420dc8740c49f492f401de0`
- 3y/5y production authority SHA-256: `14e995760ca7f7e8d75be87b7c74bf219b519a7a78b254633d732678644c13c3`
- 固定3y/5y production authority: `eligible`、required 8組すべてmetric eligible、blocking reasonなし

独立confirm artifactは診断scopeなのでartifact全体にはproduction変更権限がない。production scopeで同じ2 cohortを再評価したintegrity auditも、3yだけのrunに対する`missing_required_horizons:5y`に加え、両cohortの`priced_master_without_universe_return_unresolved`を報告した。後者はcohort固有のintegrity blockerであり、個別metricの方向安定では代替しない。

親evidenceは [`2026-08-02-margin-supply-demand-adoption.md`](./2026-08-02-margin-supply-demand-adoption.md) の固定値を再利用した。1y design / confirmのmean decile spreadは+7.28pt / +15.48pt、positive cohort shareは94.74% / 94.44%、authority eligibleな3y designは+9.85pt / 100%だった。1y両窓と3y designの全controlもmedian正・trap非正である。

## 独立3y confirm

| as-of | axis n | decile spread | best median excess | best−worst trap |
| --- | ---: | ---: | ---: | ---: |
| 2023-06-30 | 1,220 | +54.86pt | +27.99pt | −27.87pt |
| 2023-07-31 | 1,179 | +34.24pt | +9.10pt | −21.63pt |
| **単純平均 / 合計** | **2,399** | **+44.55pt** | **+18.54pt** | **−24.75pt** |

両cohortともmetric計算はresolved、production panel契約は有効、delistingとpriced-masterの`margin_short_to_adv` directionはstableだった。positive cohort shareは100%である。ただし、priced-master対象のforward return解決が完全でないため、**2 cohortともintegrity blocked**である。

| as-of | priced-master対象 | resolved target | resolution complete | integrity |
| --- | ---: | ---: | --- | --- |
| 2023-06-30 | 16 | 15 | false | blocked |
| 2023-07-31 | 10 | 8 | false | blocked |

| control | available cohort | matched weight | mean median spread | mean trap delta |
| --- | ---: | ---: | ---: | ---: |
| market cap | 2 | 1,199 | +15.41pt | −9.21pt |
| turnover | 2 | 1,199 | +15.22pt | −8.68pt |
| trailing PER | 2 | 1,058 | +10.53pt | −3.06pt |
| dividend yield | 2 | 1,111 | +8.22pt | −0.89pt |
| price level | 2 | 1,199 | +15.65pt | −8.85pt |
| 60d momentum | 2 | 1,199 | +19.46pt | −11.01pt |
| 60d realized vol | 2 | 1,199 | +9.99pt | −4.73pt |
| sector 33 | 2 | 1,136 | +5.99pt | −7.80pt |

全controlでmedian正・trap非正、2 cohort利用可能、matched weight 200以上を満たした。これは方向と標本の診断を通しただけで、integrity blockerを上書きしない。

## 固定3y/5y authority

| horizon / as-of | axis n | decile spread | best median excess | best−worst trap |
| --- | ---: | ---: | ---: | ---: |
| 3y / 2020-01-31 | 997 | +12.23pt | +3.79pt | −14.29pt |
| 3y / 2020-05-29 | 1,050 | +18.17pt | +14.40pt | −20.00pt |
| 3y / 2021-01-29 | 1,040 | +21.41pt | +18.77pt | −6.73pt |
| 3y / 2021-05-31 | 1,183 | +25.39pt | +15.32pt | −23.89pt |
| **3y 単純平均 / 合計** | **4,270** | **+19.30pt** | **+13.07pt** | **−16.23pt** |
| 5y / 2020-01-31 | 963 | +44.39pt | +22.96pt | −25.28pt |
| 5y / 2020-05-29 | 1,012 | +45.40pt | +33.90pt | −27.90pt |
| 5y / 2021-01-29 | 1,000 | +42.47pt | +25.13pt | −20.00pt |
| 5y / 2021-05-31 | 1,121 | +36.05pt | +9.67pt | −25.24pt |
| **5y 単純平均 / 合計** | **4,096** | **+42.08pt** | **+22.92pt** | **−24.61pt** |

3y / 5yともpositive cohort shareは100%で、75%下限を超えた。8組すべてでrequired metricはeligible、delisting / priced-masterのmetric directionはstableだった。

| control | 3y weight | 3y median / trap | 5y weight | 5y median / trap |
| --- | ---: | ---: | ---: | ---: |
| market cap | 2,132 | +10.72pt / −8.57pt | 2,045 | +22.31pt / −11.91pt |
| turnover | 2,132 | +8.48pt / −8.82pt | 2,045 | +19.47pt / −12.98pt |
| trailing PER | 1,739 | +8.80pt / −8.51pt | 1,669 | +13.92pt / −6.71pt |
| dividend yield | 1,988 | +3.65pt / −2.58pt | 1,905 | +5.34pt / −4.83pt |
| price level | 2,132 | +8.71pt / −8.00pt | 2,045 | +19.39pt / −10.89pt |
| 60d momentum | 2,132 | +7.95pt / −6.26pt | 2,045 | +15.54pt / −9.48pt |
| 60d realized vol | 2,132 | +7.66pt / −6.96pt | 2,045 | +17.63pt / −8.97pt |
| sector 33 | 2,029 | +7.82pt / −7.66pt | 1,941 | +19.67pt / −10.88pt |

各controlは両horizonとも4 cohortすべてで利用可能で、matched weight 400以上、mean median正、mean trap非正だった。

固定4 as-ofの`in_population`に対する正の`margin_short_to_adv` coverageは83.95%、85.15%、84.77%、86.85%、平均85.18%で、60%下限を超えた。貸借対象外、ADV欠損・非正、ADV窓内の分割・併合は0へ補完していない。

## 独立検算

`2020-01-31`の5y cohortをpanel / forward CSVから、evaluation helperを使わず再計算した。resolvedな流動性母集団のmedian returnは19.40%、axis nは963、decile sizeは`96, 96, 96, 97, 96, 96, 97, 96, 96, 97`だった。

- worst decileの空売り残/ADV: 11.5205日から1.6989日、median excess −21.44%、trap 52.08%
- best decileの空売り残/ADV: 0.1126日から0日、median excess +22.96%、trap 26.80%
- decile spread: +44.3942pt
- market-cap control: 5 strata、matched weight 480、median spread +8.1588pt、trap delta −5.2140pt

axis n、decile境界とsize、best / worst、spread、controlはartifactと丸め後まで一致した。

## 採否と運用境界

| 条件 | 結果 |
| --- | --- |
| 親evidence | pass |
| 独立3y confirmの方向・trap・control・標本 | pass |
| 独立3y confirmのintegrity | **fail（priced-master return unresolved）** |
| required 3y/5y authority 8組 | pass |
| delisting / priced-master sensitivity | pass |
| 3y/5yの方向・trap・control・標本 | pass |
| 正の値のcoverage 60%以上 | pass（平均85.18%） |
| 最終判定 | **`insufficient`、production変更なし** |

再判定は同じ2 cohortのpriced-master対象を完全に解決できた場合に限る。良い固定3y/5y結果や個別metricのsurvivorship安定だけでintegrityを読み替えず、別cohortへの差し替えもしない。
