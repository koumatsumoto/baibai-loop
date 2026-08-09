---
title: "会社予想改定・複数期加速レーンの長期較正"
summary: "会社予想の上方改定とmargin加速は候補を作れたが、exact-sector matched boundaryの被覆が全窓で不足し、診断値も一貫して非採用方向だったためinsufficientと判定する。"
doc_type: measurement-record
status: active
date: 2026-08-10
---

# 会社予想改定・複数期加速レーンの長期較正

価値tier: T1 — core E[r] と異なる fundamental inflection が、core top-20 外から trap 非劣後かつ正の増分価値を持つ候補を安定供給できるかを、production 配線前に判定する。

## 結論

**判定は `insufficient`。production の E[r]、FV、rank、gate、selection payload は変更しない。**

全4窓で inflection top-20 と core 外 top-5を構成できたが、事前登録した exact-sector matched core boundary の被覆率は18.6%〜31.6%にとどまり、floor 75%を全窓で外した。1y time holdout と両3y窓は reported pair 32件にも届かない。不足は未満期 cohort ではなく比較設計の構造にあるため、専用 evaluator と test は通常 tree に残さない。sector fallback、match pool拡大、signal閾値変更、別weight、短期horizonは試さない。

sufficiency 外の診断では、ticker等重みの inflection annualized excess と pair delta が全窓の reported / 全損 / 中立で負だった。inflection trapも全caseで両比較対象より高い。これは採用方向への証拠ではないが、sufficiency gateを飛び越えて正式な `negative` ともしない。

## 実行 identity

- 事前登録 commit: `103af55a9551cea26c93e5afd65190f3abbbee24`
- 評価時 cache schema: `14`
- panel / forward: 80 / 80 cohort（2019-11-29〜2026-06-30）
- screening rules hash: `f4a3f3f20838e1cd`（80 panel 全件一致）
- build: panel 80件・301,764行、forward 1,509,220行、resolved 1,055,260行
- core evaluation SHA-256: `241b4d3c50d55014de14be8b98b766c388d15629d4c54625fa44d012e3fb64d5`
- inflection evaluation SHA-256: `ca858552011cdb4dc32408c2a9d7607da87016fc64fc701d68ba6d10f4fcf648`

inflection artifact は全 membership、match identity、選択対象の return status、reported / 全損 / 中立の個別 pair、core top-20 trap観測、窓集計、window / overall verdictを持つ。評価後に別variant、窓、重み、fallback、閾値を試していない。

## Coverage と候補形成

`in_population >= 100` の78 panelで、4 signal同時測定率は35.1%〜67.7%（中央値52.8%）、上方改定gate後は5.8%〜19.3%（中央値12.5%、27〜290銘柄）だった。全窓で eligible 20件以上と core外 top-5を構成できる。

事前coverage表の4列同時最小値35.4%に対し、評価用schema 14再構築値は35.1%だった。forward参照前に同一 fiscal periodの最新訂正行を優先し、欠損時に古い開示へ戻らない契約で再生した結果である。事前登録commitはこの実装を含むが、coverage表の再集計値は反映していない。差は事前固定した10% gate、20件、match、effect条件を変えない。

全80 membership は `matched` 20、`match_unavailable` 58、初期2 cohortの `candidate_unavailable` 2だった。exact-sector match数は0〜5で、内訳は0件4 cohort、1件5、2件29、3件22、4件13、5件7である。

## 窓別 sufficiency

| window | eligible | candidate | matched / candidate | reported / membership pair | gate / population median | unique inflection / pair | max share | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `1y_design` | 43 | 43 | 8 / 43 (18.6%) | 35 / 35 (100%) | 12.76% | 81 / 30 | 3.7% | `insufficient`: match coverage |
| `1y_time_holdout` | 23 | 23 | 7 / 23 (30.4%) | 30 / 30 (100%) | 12.25% | 48 / 25 | 7.8% | `insufficient`: match coverage、pair < 32 |
| `3y_all` | 33 | 33 | 7 / 33 (21.2%) | 27 / 31 (87.1%) | 14.46% | 70 / 26 | 4.2% | `insufficient`: match coverage、pair < 32 |
| `3y_postcovid` | 19 | 19 | 6 / 19 (31.6%) | 23 / 27 (85.2%) | 15.51% | 46 / 22 | 5.3% | `insufficient`: match coverage、pair < 32 |

候補coverage、上方改定gate比率、unique ticker / pair、集中上限は通る。比較対象だけが不足しており、同sector・rank 11〜30・未使用という境界を緩めず停止する。

## Effect 診断

sufficiencyを通らないため、以下は採用根拠に使わない。値は `inflection annualized excess / pair delta / positive share / traps: inflection / matched / core top-20` の順である。

### 主読み: inflection ticker 等重み

| window | case | excess | delta | positive | traps |
| --- | --- | ---: | ---: | ---: | ---: |
| `1y_design` | reported | −6.05pt | −25.31pt | 40.0% | 38.3% / 13.3% / 8.0% |
|  | total loss | −6.05pt | −25.31pt | 40.0% | 38.3% / 13.3% / 8.6% |
|  | neutral | −6.05pt | −25.31pt | 40.0% | 38.3% / 13.3% / 8.0% |
| `1y_time_holdout` | reported | −4.72pt | −0.73pt | 46.7% | 36.7% / 16.7% / 19.6% |
|  | total loss | −4.72pt | −0.73pt | 46.7% | 36.7% / 16.7% / 20.8% |
|  | neutral | −4.72pt | −0.73pt | 46.7% | 36.7% / 16.7% / 19.3% |
| `3y_all` | reported | −16.99pt | −21.81pt | 29.4% | 64.7% / 13.7% / 9.4% |
|  | total loss | −17.10pt | −21.81pt | 31.6% | 68.4% / 26.3% / 17.0% |
|  | neutral | −16.99pt | −17.10pt | 31.6% | 63.2% / 17.5% / 8.7% |
| `3y_postcovid` | reported | −18.16pt | −21.81pt | 30.8% | 69.2% / 17.9% / 12.3% |
|  | total loss | −18.16pt | −21.81pt | 33.3% | 73.3% / 33.3% / 20.4% |
|  | neutral | −17.10pt | −17.10pt | 33.3% | 66.7% / 22.2% / 11.2% |

### 副読み: cohort 等重み

| window | case | excess | delta | positive | traps |
| --- | --- | ---: | ---: | ---: | ---: |
| `1y_design` | reported | −13.02pt | −21.79pt | 12.5% | 39.4% / 18.8% / 7.0% |
|  | total loss | −13.02pt | −21.79pt | 12.5% | 39.4% / 18.8% / 7.5% |
|  | neutral | −13.02pt | −21.79pt | 12.5% | 39.4% / 18.8% / 6.9% |
| `1y_time_holdout` | reported | +5.79pt | −2.58pt | 42.9% | 30.0% / 12.9% / 23.1% |
|  | total loss | +5.79pt | −2.58pt | 42.9% | 30.0% / 12.9% / 23.6% |
|  | neutral | +5.79pt | −2.58pt | 42.9% | 30.0% / 12.9% / 22.9% |
| `3y_all` | reported | −11.22pt | −17.01pt | 14.3% | 59.8% / 17.9% / 11.3% |
|  | total loss | −5.72pt | −3.88pt | 28.6% | 57.9% / 29.3% / 17.1% |
|  | neutral | −5.72pt | −3.88pt | 14.3% | 54.3% / 20.7% / 10.7% |
| `3y_postcovid` | reported | −14.24pt | −19.41pt | 16.7% | 61.4% / 20.8% / 13.2% |
|  | total loss | −11.41pt | −2.92pt | 33.3% | 59.2% / 34.2% / 19.2% |
|  | neutral | −8.81pt | −3.71pt | 16.7% | 55.0% / 24.2% / 12.5% |

ticker等重みでは全窓・全caseで excessとdeltaが負で、trap差も大きい。cohort等重みで唯一正の excessだった1y time holdoutだけを採らず、月次反復を1銘柄1票へ畳む主読みと全条件判定を維持する。

## 独立検算

評価moduleを importせず、panel / forward CSVを標準CSV readerで読み、4列gate、tieを分割しないmidrank、inflection score、top-20、core外top-5、greedy exact-sector match、population median、annualization、pair delta、trapを再実装した。代表1y / 3y cohortのmembership全体と全reported pairがartifactに一致した。

`2020-06-30` / `1y`:

- population 1,317、4列測定556、eligible 93、match 4、reported pair 4
- 代表match `2440 → 9743`
- population return median +15.2563%
- pair delta **−44.3830pt**
- `2440` はtrap、`9743` は非trap

`2021-10-29` / `3y`:

- population 1,337、4列測定785、eligible 229、match 4、reported pair 3
- 代表match `7581 → 2678`
- population cumulative return median +8.0675%
- pair delta **+14.7124pt**
- 両tickerとも非trap

## 誠実性の限定

- 1y forward窓はcalendar上で重なり、月次cohortを独立標本とは扱えない。`3y_postcovid` は `3y_all` の部分集合で独立confirmではない。
- returnはsplit-adjusted `price_return_only`で、配当を含まない。
- entryが2020年前後へ偏る3y窓は、日本株上昇局面とCOVID後のregimeに依存する。
- inflection入力はas-of以前2,200暦日、最新観測400暦日以内に限る。古い開示を無期限carry-forwardしない。
- SQLiteは同日訂正前の版を保持せず、訂正理由も保持しない。保存された最新行をその日の観測とする。
- margin加速は同じfiscal periodとexact fiscal year endだけを使う。決算期変更を近似せず、CFOと営業利益で採る最新periodは異なりうる。
- matchはinflection score順のgreedy without replacementで、global optimal matchingではない。
- 同じtickerが月次で反復するためcohort等重みは集中の影響を受ける。主読みはticker等重みである。
- coverage診断と閾値はforward outcome前に固定したが、既存screening構造を知った上で設計した非盲検diagnosticである。
- effect表は構造的sufficiency failure下の記述統計で、有意性、因果、将来収益を主張しない。

## Cleanup と production authority

不足はexact-sector matchとresolved pair数という構造要因で、未満期cohortだけではない。専用evaluatorとtestを削除し、再評価taskやproduction配線issueは作らない。4つの派生列、導出policy、PanelRow / store契約も、現在の利用者とT1〜T3への実現経路がないため最終treeから削除する。PIT導出規則と反証結果は事前登録commit、dated report、artifact hashに固定し、通常のcalibration buildへ恒久的な計算・schema保守を持ち込まない。

application DB、market store、runs store、production selectionには書き込んでいない。評価にはローカルschema 14 cacheだけを使い、cleanup後のローカルcalibration storeは現行schema 13へ再構築した。R2へpushしていない。
