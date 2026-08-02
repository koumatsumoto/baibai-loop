---
title: "投資有価証券による資産バリュー軸の事前登録"
summary: "EDINETの投資有価証券簿価とnet cashを使うasset-backed ratioを、PBR統制と還元変化との交互作用で評価する。"
doc_type: report
status: active
date: 2026-08-02
---

# 投資有価証券による資産バリュー軸の事前登録

価値tier: T1 — cash-rich候補の下値保護を現金だけでなく投資有価証券簿価まで観測し、資産が還元へ動く候補の発見面積を広げる。

## 1. 目的と変更権限

本計測は次の2仮説を固定して検証する。

- H-1: `asset_backed_ratio = (net_cash + investment_securities) / market_cap` は、高いほど将来returnが良く、PBRを統制した後も残存効果を持つ。
- H-2: `asset_backed_ratio >= 0.4` の資産厚銘柄では、`shareholder_return_change == true` の層がfalseの層より将来returnとtrap率で優位になり、資産が厚いだけの慢性割安と区別できる。

本issueの権限は、EDINET抽出field、ranking-neutralなcandidate metric、panel軸、evaluation診断、research checklistまでとする。cash-rich playbook、screening rules、E[r]、FV、ranking、warning/badgeは変更しない。H-1/H-2が全条件を満たす場合だけ、playbook variantのrules変更を別issueへ送る。

## 2. outcome非参照で確認したavailability

forward returnと既存calibration outcomeは参照せず、source期間、raw CSV tag、直近入力分布だけを確認した。

- `edinet_metrics`: 2026-05-08〜2026-07-31、27 snapshots、107,100行
- `edinet_documents`: 2024-07-31〜2026-07-31、485日、166,666行
- disposable CSV ZIP: 4,021 files
- CSV内で`投資有価証券`を含むZIP: 3,465、exact BS tag `jppfs_cor:InvestmentSecurities`の数値をconsolidation basis上で取得できるZIP: 3,018
- 2026-06-30の流動性population: 1,505、`net_cash`・時価総額・exact BS tagを結合できるrow: 1,044（69.37%）
- 同populationの`asset_backed_ratio`: p10 −62.27%、median +6.97%、p90 +42.75%
- 固定閾値0.4の入力群: asset-thick + return-change 107、asset-thick + no-change 18、thin + change 721、thin + no-change 198

既存のEDINET metric snapshotはforward outcomeを持つ歴史cohortに存在しない。documentsも540日lookbackを満たす最古as-ofが2026年になり、現在時点では1y design / confirmと3y評価を構成できない。したがって、外部backfillなしの本issueで得られる最大判定は`inconclusive`である。このavailability条件を、実装後にoutcomeを見て緩和しない。

## 3. 抽出fieldと派生軸

### `investment_securities`

- sourceはEDINET `type=5` CSVのBS数値fact、local nameがexactに`InvestmentSecurities`の要素だけとする。
- consolidated rowが存在すればconsolidatedだけを使い、無い場合だけnon-consolidatedへfallbackする。context rankingは既存のcash/equityと同じcurrent・primary優先を使う。
- 数値0とzero-like表示は0として保持し、tag不在、非数値、負値、非有限値はnullまたはvalidation errorにする。
- `SharesOfSubsidiariesAndAssociates`等の関係会社株式、`OperationalInvestmentSecurities`、短期`Securities`、text block、売却損益・評価損益・CF項目は含めない。
- fieldは簿価grossである。投資有価証券には未上場・低流動性の内訳が含まれ得るため、市場性、時価回収額、売却可能性を機械で断定しない。

### `asset_backed_ratio`

- `asset_backed_ratio = (EDINET net_cash + EDINET investment_securities) / market_cap`
- market cap、net cash、investment securitiesのいずれかが欠ける場合、market capが非正、または値が非有限ならnullとする。
- ratioは負値を許容する。負のnet cashが投資有価証券を上回る状態を0へ丸めない。
- `investment_securities`と`asset_backed_ratio`はobserved / derivedのcandidate contextとして出すが、割安の事実、清算価値、FVとは呼ばない。

固定閾値0.4は既存cash-richの`cash_to_market_cap >= 0.4`と同じ下値保護比率であり、直近入力で両interaction群を形成できる。outcome結果に応じて0.3 / 0.5等へ動かさない。

## 4. H-1の評価とcontrol

`asset_backed_ratio`は高いほど良い (`direction=1`) とする。全流動性populationでdecile spreadを測る。

controlは順序を固定する。

1. `pbr`（必須）
2. `market_cap_oku`
3. `equity_ratio`

各controlのstrata内でasset-backed ratioの良い半分−悪い半分を比較する。PBRの代理にすぎない場合を先に棄却する。後から都合の良いcontrolを追加・削除しない。

## 5. H-2のinteraction

各cohortを次の4群へ固定する。ratioまたは`shareholder_return_change`がnullのrowは補完せず除外する。

- A: `asset_backed_ratio >= 0.4` and `shareholder_return_change == true`
- B: `asset_backed_ratio >= 0.4` and `shareholder_return_change == false`
- C: `asset_backed_ratio < 0.4` and `shareholder_return_change == true`
- D: `asset_backed_ratio < 0.4` and `shareholder_return_change == false`

主比較はA−Bのmedian excessとtrap rate。資産厚だけでなく還元変化が伴うことの増分を見る。副比較としてdifference-in-differences `(A−B) − (C−D)` を出し、還元変化一般の代理でないか確認する。副比較だけで主判定を上書きしない。

## 6. 固定時間分割とavailability gate

EDINET historyを追加できた場合の1y窓は、保存documents期間内のcohortを次のように固定する。

| horizon | design | confirm |
| --- | --- | --- |
| 1y | 2024-07-31〜2024-12-30 | 2025-01-31〜2025-06-30 |

3yは現在のsource historyでは成立しない。将来または別途backfillで評価可能になった場合も、design / confirmをoutcome参照前に別の事前登録で固定する。本reportの1y窓を3yへ流用しない。

各1y窓は次を満たす場合だけ効果判定する。

- resolved cohortが各6以上
- `asset_backed_ratio`のcohort平均coverageが50%以上
- H-1 eligible合計が各窓1,000以上
- H-2でA合計100以上、B合計30以上、A/B各5以上の比較可能cohortがresolved cohortの60%以上

不足時は`inconclusive`とし、閾値緩和、群統合、null補完をしない。

## 7. 採否条件

`adoption_candidate`は次をすべて満たす場合だけとする。

### H-1

1. availability gateをdesign / confirmの両方で通過する。
2. 両1y窓でmean decile spreadが+3pt以上、positive cohort shareが60%以上。
3. 必須PBR controlのstratified median spreadが+2pt以上、trap deltaが0以下。
4. 規模・equity ratio controlはmedian spreadが正、trap deltaが0以下。
5. 別途事前登録した3y design / confirmの両方でspread、PBR controlが正、trapが非悪化。

### H-2

1. availability gateをdesign / confirmの両方で通過する。
2. 両1y窓でA−B median excessが+3pt以上、positive cohort shareが60%以上、trap deltaが−3pt以下。
3. 両1y窓でdifference-in-differencesのmedian差が正。
4. 別途事前登録した3y design / confirmでA−B median差とdifference-in-differencesが正、trapが非悪化。

現在は3y availabilityが無いため、性能値にかかわらず`adoption_candidate`にはしない。availabilityだけを外す場合は`inconclusive`、十分なdataで方向・効果量を外す場合は`negative`とする。

## 8. 実装・検算境界

- extractor revisionはparser/model/schema変更を含めて更新され、旧snapshotを新fieldありとして差分再利用しない。
- SQLite migration、read/write、baseline reuse、candidate record、panel cache schemaを同時に進め、missing columnやprovenance混在をfail closedにする。
- negative testでexact tag、関係会社株式除外、operational/CF/損益tag除外、consolidated優先、zero、missing、負値、非有限値、market cap非正、cache schema mismatchを固定する。
- AP-08へinvestment securitiesとasset-backed ratioのvalidator checklistを追加する。
- research checklistには、簿価内訳、上場/未上場、時価、税haircut、売却制約、還元への接続を一次書類で確認する項目を追加する。
- current cohortをraw CSV ZIP→SQLite→metrics→candidate/panelまで別計算し、分子とratioを検算する。
- dated result reportはavailability、current coverage、実装結果、判定`inconclusive`を残す。outcomeを持たないcurrent cohortのreturnを推測しない。

## 9. 想定コマンド

```bash
.venv/bin/baibai-engine screening extract-edinet-metrics \
  --asof 2026-06-30 --lookback-days 540
.venv/bin/baibai-engine screening calibration-build \
  --start 2019-11-01 --end 2026-07-31 --force
```

EDINET documents / CSV ZIPがlocal cacheに揃うas-ofだけを再抽出する。欠けたsourceを暗黙にprovider networkへ取りに行かず、必要なら承認経路で明示する。
