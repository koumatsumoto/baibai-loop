---
title: "投資有価証券による資産バリュー軸の計測結果"
summary: "EDINET抽出と資産厚・還元変化の診断経路を実装したが、forward outcome期間に入力履歴がなくinconclusiveと判定した。"
doc_type: report
status: active
date: 2026-08-02
---

# 投資有価証券による資産バリュー軸の計測結果

価値tier: T1 — cash-rich候補の下値保護を現金だけでなく投資有価証券簿価まで観測し、資産が還元へ動く候補の発見面積を広げる。

## 結論

- **H-1 / H-2 はともに `inconclusive`。** 固定した1y design / confirm各6 cohortはresolvedだが、`asset_backed_ratio` coverageがいずれも0でavailability gateを満たさない。3y入力履歴も存在しない。
- EDINET `InvestmentSecurities` exact tagから帳簿価額を抽出し、`asset_backed_ratio = (net_cash + investment_securities) / market_cap`をcandidate context、production panel、評価診断へ接続した。screening rule、E[r]、FV、ranking、warningは変更していない。
- current cohortはcoverage 71.16%で軸を形成できるがforward outcomeが未確定である。current分布や4群の件数から将来returnを推測せず、閾値0.4も動かさない。
- adoption / rules変更のfollow-upは作らない。十分なpoint-in-time EDINET historyと、別途事前登録した3y design / confirmが揃うまで再判定しない。

## 1. 固定した実行契約

定義、exact tag、除外tag、固定control、4群、時間分割、availability gate、採否条件は、outcome計測前のcommit `757c450` と [`2026-08-02-hidden-assets-preregistration.md`](.../2026-08-02-hidden-assets/preregistration/report.md) で固定した。実装は`2bedb80`、生成panelの丸め済み時価総額を正確なratioと照合するreader契約は`03764fc`で固定した。

- market SQLite schema: `17`
- calibration cache schema: `10`
- extractor revision: `751e098c97f9ae4dde7c4d3a39d691846d276b16b5451043ee56a1600f0f2ae2`
- production rebuild: 80 panels、forward 1,509,220行、resolved 1,055,260行
- current panel SHA-256: `80616680a1205b4a94bf67dbf0e632d12fdc104d523dd0ee08d06244749cbe52`
- 1y design evaluation SHA-256: `513d285c19f3226867ceed40e03b731b8ffee97f1de5894cdb323805597536e4`
- 1y confirm evaluation SHA-256: `c14bb89efa9e944971b08d0b555798f5c10d3845536151b249c09957f899e5cb`

## 2. EDINET抽出とcurrent availability

2026-06-30をas-ofとして541日分のdocument eventを列挙し、128,556 documentsから3,969 filingを選定した。extractor revisionが変わったため旧metricを再利用せず、3,969 filingを再解析した。

| 項目 | 結果 |
| --- | ---: |
| selected / parsed | 3,969 / 3,969 |
| hard parser failure | 0 |
| `investment_securities` non-null | 2,990 |
| 明示zero | 30 |
| 負値 | 0 |
| extractor revision数 | 1 |

`quality_issues=1,958`は既存metricを含むtag欠損annotationの件数で、hard parser failureではない。投資有価証券が取れないrowは0へ補完せずnullを保持する。

2026-06-30の流動性populationは1,505、`asset_backed_ratio` non-nullは1,071でcoverage 71.16%。分布はp10 −61.96%、median +6.96%、p90 +42.19%だった。

| 固定群 | 条件 | n |
| --- | --- | ---: |
| A | asset-thick + return-change | 110 |
| B | asset-thick + no-change | 11 |
| C | thin + return-change | 740 |
| D | thin + no-change | 106 |

input-only scanとの差はcanonical document selectionと全parserを通した結果である。Bはcurrent単一断面でも事前登録した合計30の下限へ届かないが、この断面件数を理由に群統合や閾値変更は行わない。

## 3. H-1 / H-2 availability gate

固定1y窓をproduction panelで評価した。

| window | resolved cohort | mean coverage | A / B / C / D | comparable cohort | 判定 |
| --- | ---: | ---: | ---: | ---: | --- |
| design 2024-07-31〜2024-12-30 | 6 | 0.00% | 0 / 0 / 0 / 0 | 0 | inconclusive |
| confirm 2025-01-31〜2025-06-30 | 6 | 0.00% | 0 / 0 / 0 / 0 | 0 | inconclusive |

両窓はforward returnを解決できるが、そのas-ofの`edinet_metrics` snapshotが存在しない。現在のEDINET documents保存期間を使った再抽出はcurrent source stateであり、過去as-of factとして遡及利用しない。coverage 50%、H-1 eligible 1,000、H-2 A/B件数と比較可能cohort比率の全条件を外すため、効果量・control・trap・difference-in-differencesは算出しない。

3yは入力履歴自体が無く、1y窓を流用しない。性能値を観測していないため`negative`とも判定しない。

## 4. rawからcandidate / panelまでの検算

1301をraw SQLiteから別計算した。

- `net_cash`: −61,054,000,000円
- `investment_securities`: 21,269,000,000円
- close: 4,370円
- split-adjusted shares: 12,078,283株
- market cap: 52,782,096,710円
- `(−61,054,000,000 + 21,269,000,000) / 52,782,096,710 = −0.753759370693253`

panelの`asset_backed_ratio = −0.7537593706932526`と一致した。流動性snapshotの`market_cap_oku`は528億円に丸められるため、cache readerは正確なratioを丸め値の±0.5億円区間で検証する。current populationの1,071 non-null行は全件この区間内で、outsideは0だった。

一時runs DBでcache-only screening runも行い、candidate YAMLに1301の`investment_securities = 21,269,000,000`、`asset_backed_ratio = −0.7538`が出ることを確認した。runは既存TTM品質欠損によるpartial warningを返したが、universe 3,713 / candidates 3,713を生成し、新fieldの欠損・shape errorは無かった。

## 5. 実装境界と限界

- 投資有価証券は簿価grossで、上場 / 非上場、時価、含み損益、税、流動性、持合い・契約・事業上の売却制約を反映しない。marketable / liquid / fair valueや清算価値とは呼ばない。
- research checklistで内訳、時価、税haircut、売却制約、株主へ届く経路を一次資料から確認する。
- `InvestmentSecurities`以外の関係会社株式、営業投資有価証券、包括的な`Securities`、売却損益・CF・text blockは合算しない。
- current cohortのforward outcomeが解決しても、confirm分割と3y authorityが無いため、それだけでadoptionへ進めない。
