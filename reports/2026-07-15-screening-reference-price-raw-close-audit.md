# screening参考価格とraw closeの丸め誤差監査（#432）

## 0. 目的と事前登録

本監査の目的は、candidate reportの`screening参考価格`がJPX raw/unadjusted closeとどの程度ずれ、人間がprimary-research setを選ぶときのFV gap表示をmaterialに変えるかを測ることである。発注価格は`plan-limit`がraw closeを別途取得するため、約定価格や運用成績は対象にしない。

以下の比較basis、式、coverage stop、採否基準を全ranking-eligibleのconfirm計測より先に固定する。既存の2026-07-10 / 2026-07-14 audit top-20については予備probeで結果の大小を既に見ており、本判定はblindではない。閾値は予備値へfitさせず、丸め誤差の数学上限と人間に見えるFV gapのpercentage point差から決める。有意性、統計的優位、track recordは主張しない。

事前登録commit時点で、confirm結果は未計測である。結果節の`TBD`は事前登録後の別commitで埋め、採否基準は変更しない。

## 1. 現行contractと評価対象

[`build_universe`](../src/baibai_loop/screening/universe.py)は各tickerの最新raw closeとsplit-normalized sharesから時価総額を作り、`market_cap_oku = round(raw_close * shares / 1e8)`と整数億円に丸める。[`_screening_reference_close_yen`](../src/baibai_loop/screening/selection/summaries.py)は、その丸め済み値を`round(market_cap_oku * 1e8 / shares, 4)`で株価へ戻す。この不可逆なround-tripが通常の差の原因である。

一方、[`estimate_expected_return`](../src/baibai_loop/screening/estimates.py)が使うFV anchorとE[r]は丸め前のraw closeから作られる。[`build_selection_payload`](../src/baibai_loop/screening/selection/payload.py)のaudit rankは`er_annual`降順、playbook順、evidence strength、tickerで確定した後に表示用`market_price_yen`を付与する。したがってreferenceをraw closeへ差し替えてもproduction audit rankとtop-20 membershipは変わらず、影響件数はcontract上0である。FV-gap順の仮想rerankはproductionに存在しないため行わない。

機械計測でもselection sort keyにreference priceが含まれないことと、audit rowのreferenceをraw closeへ差し替えても保存済みrankが全件不変であることを確認する。主な実測指標は価格誤差とFV gap表示差である。

## 2. 計測母集団とinput provenance

confirmは2026-07-14の最新完全screening artifactに現行rulesのselection gateを適用した**全ranking-eligible**を母集団とする。audit top-20だけを時系列にサンプルするより、現在の表示contractが適用され得る全行のtailと最大差を直接検査できるためである。

ranking-eligibleは現行selectionと同じく、market cap、ADV、listing span、required JPX flagsのliquidity predicateを通過し、`metrics.er_annual`が欠損しないcandidateとする。evidence hitの有無はranking gateに追加しない。

| input | as-of / scope | SHA-256 / metadata | use |
| --- | --- | --- | --- |
| `.cache/opportunity/2026-07-14/candidates.yaml` | 2026-07-14 / all common stocks | `66c1b230ee36a60c6690bf4a73fb6d30673ac38b11568db34931ff36bd6fa80d` | confirm母集団とreference再構成 |
| `.cache/opportunity/2026-07-14/selection-output.yaml` | 2026-07-14 / audit top-20 | `e83718cfc01115405be7d82f8d98b4643e4400412bdb1a663c101127245404e2` | current top-20 warning判定とartifact reference検算 |
| `.cache/opportunity/2026-07-10-attempt-2/candidates.yaml` | 2026-07-10 / final retry input | `ba1359fe3bca102cae61726b86828ee69a2505de9aa73f7a6ebf85289776bd50` | operational pilot |
| `.cache/opportunity/2026-07-10-attempt-2/selection-output.yaml` | 2026-07-10 / final retry audit top-20 | `3f2327f05fe649ccad7190750c6a2036e3837f921bc4108f4f53e6dadd875208` | operational pilot |
| `records/_config/screening-rules/2026-07-06T000000+0900.yaml` | current rules | `bd4bba8ceb74d08fefb4b670ddf28f2e19b8368679678237599be453f214c103` | ranking-eligible predicate |
| `data/screening/market.sqlite` | raw bars 2021-08-02..2026-07-14 | `user_version=12`, 5,219,443 bar rows | exact-date raw close / factor |

計測時のrepository baselineは`a2775e7db22fd1d258afface04a69f6198bc73c5`。SQLiteのsecurity masterは2026-05-13の1 snapshotのみであり、これより前のproduction audit poolはpoint-in-timeに再構成できない。本監査はcurrent cross-sectionと保存済みoperation artifactだけを使い、latest masterを過去に当てるbackfillを行わない。

## 3. 事前登録する比較basisと計算式

観測単位は`ticker-asof`とする。referenceはartifactの現行式から復元し、audit top-20ではartifactの`market_price_yen`と4桁小数まで一致することを求める。rawはSQLiteの`ticker`と`traded_at = asof`が完全一致する`close`だけを使い、`adjustment_close`と古いbarによるfallbackを使わない。

```text
reference_price_yen = round(market_cap_oku * 1e8 / shares_outstanding, 4)
signed_price_error_pct = 100 * (reference_price_yen / raw_close_yen - 1)
abs_price_error_pct = abs(signed_price_error_pct)

fv_gap_reference_pct = 100 * (fair_value_anchor_yen / reference_price_yen - 1)
fv_gap_raw_pct = 100 * (fair_value_anchor_yen / raw_close_yen - 1)
fv_gap_delta_pp = fv_gap_raw_pct - fv_gap_reference_pct
abs_fv_gap_delta_pp = abs(fv_gap_delta_pp)
```

primary comparableはreference、raw close、shares、FV anchorが正でfinite、同日barの`adjustment_factor = 1`、candidateの`split_adjustment_flag = false`である行とする。欠損、non-1 / null factor、split flagは別表に残し、adjusted priceで補完せずprimary分布から除く。

medianとp95は丸め前のabsolute値で計算する。p95は補間を使わないnearest-rankとし、`sorted(values)[ceil(0.95 * n) - 1]`で固定する。表示では十分な桁へ丸めるが、採否は丸め前値で決める。

計測はrepository rootから次の通り実行した。scratch scriptとJSONはlocal-onlyでありGit管理しない。判定の正本は本reportの式・集計値・検算である。

```bash
.venv/bin/python .cache/432/reference_price_audit.py
sha256sum .cache/432/reference_price_audit.py .cache/432/result.json
```

- scratch script SHA-256: `a09eef003662701f953baefdd68a8ab901e232c30e7e3edbff10312f0b8fd3c8`
- result JSON SHA-256: `fa785fed7b8d190d7a8fc99507ed908415ba6da6908e5e5f694024ebe5662395`

### 3.1 数学上のrounding bound

整数億円への丸め後market capを`M`億円、丸め前を`C`億円とすると、通常の最寄り丸めで`|M - C| <= 0.5`である。reference/raw比はsharesが相殺して`M / C`なので、selectionの丸め後floor `M >= 100`の範囲で相対誤差最大は境界`M = 100, C = 99.5`のときの次の値になる。

```text
100 * (100 / 99.5 - 1) = 0.5025125628...%
```

0.55%超は整数億円丸めだけでは説明できない。その場合はdual-price表示の根拠ではなく、shares、corporate action、reference reconstructionのsource basis defectとして停止・調査する。

### 3.2 AP-02独立検算

confirm計測後、absolute price errorが大きい行と1行の価格誤差が小さい行の2tickerを選び、SQLite raw row、market cap、shares、FVから上記式を独立に電卓再計算する。scratch outputとの一致許容差は価格`0.0001円`、percentage系`0.000001`とする。

## 4. 事前登録する採否基準

confirmはranking-eligible 100件以上かつexact primary comparable coverage 95%以上を必須とする。未達は`inconclusive / production変更なし`とする。

| judgment | 結果を見る前に固定する条件 | action |
| --- | --- | --- |
| `raw close追加表示` | confirmの`p95(abs_fv_gap_delta_pp) >= 1.0` | rendererは変えず、最小表示案を別Issueへ分離 |
| `basis defect / inconclusive` | clean rowに`abs_price_error_pct > 0.55` が1件でもある | source basisを別Issueで調査し、dual-priceで隠さない |
| `row warning` | global表示条件は未達だが、current audit top-20に`abs_fv_gap_delta_pp >= 1.0`、FV gap符号反転、raw/factor unresolved、または既存split warningで識別不能なmaterial corporate action行がある | 行単位の最小warning案を別Issueへ分離 |
| `現状維持` | coverage条件を満たし、上の3条件がすべて不成立 | candidate reportへの表示・warningを追加しない |

`p95(abs_fv_gap_delta_pp) >= 1.0`は、nearest-rank上、少なくとも約5%の表示対象でFV乖離が1 percentage point以上動く系統的差である。HTMLはFV gapを整数%表示するが、単なる四捨五入境界跨ぎはこの基準に使わず、表示変化件数だけをdiagnosticとして併記する。

## 5. 結果（事前登録後に固定）

### 5.1 coverageと分布

2026-07-14 candidates 3,744件にpublic `select`と同じgateを適用すると、liquidity通過1,476件からE[r]欠損1件を除いた1,475件がranking-eligibleとなった。public `select`の`selection.counts.after_er_filter: 1475`とscratchの抽出数は一致した。

21件は`split_adjustment_flag: true`のため事前登録どおりprimary分布から除いた。これらを含め全1,475件でexact-date raw closeは取得でき、`adjustment_factor` は1.0だった。primary comparableは1,454件、coverageは98.5763%で、95%の事前条件を通過した。

| scope | rows / primary | coverage | abs price error median / p95 / max | abs FV-gap delta median / p95 / max | 整数%表示変化 | FV-gap符号反転 | 0.55%上限超え |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-07-14 全ranking-eligible confirm | 1,475 / 1,454 | 98.5763% | 0.012546% / 0.129458% / 0.386494% | 0.008007pt / 0.093253pt / 0.551165pt | 41（2.8198%） | 1（0.0688%） | 0 |
| 既知current top-20除外sensitivity | 1,455 / 1,434 | 98.5567% | 0.012425% / 0.129458% / 0.386494% | 0.007932pt / 0.091292pt / 0.551165pt | 40 | 1 | 0 |
| 2026-07-14 current audit top-20 | 20 / 20 | 100% | 0.026611% / 0.102641% / 0.165981% | 0.025918pt / 0.126424pt / 0.369370pt | 1 | 0 | 0 |
| 2026-07-10 final-retry pilot top-20 | 20 / 20 | 100% | 0.022476% / 0.085245% / 0.224247% | 0.019499pt / 0.126759pt / 0.511128pt | 2 | 0 | 0 |

confirmの`p95(abs_fv_gap_delta_pp) = 0.093253`はraw close追加表示基準1.0ptの9.4%で、最大値0.551165ptも基準未満だった。価格誤差の最大0.386494%は数学上限0.502513%とstop基準0.55%の内側にある。予備probeで既知のcurrent top-20を除いたsensitivityでもFV-gap差p95は0.091292ptで、採否は変わらない。

### 5.2 audit top-20の意思決定影響

2026-07-14 / 2026-07-10とも、audit artifactの`market_price_yen`とcandidateから再構成したreferenceは20件すべて`0.0001円`以内で一致した。さらにproductionの`build_selection_payload`を同一candidates / rulesで独立再実行し、保存artifactと20件のrank・ticker・referenceが全件一致した。artifactの20件完備、rank連番、reference一致、production rebuild一致を確認し、contract gateはpassした。referenceはproductionのrank確定後に付与される表示fieldであり、raw closeとの差はrank inputを変えない。

current top-20にはFV-gap差1.0pt以上、符号反転、raw/factor unresolved、未識別のmaterial corporate action行はなく、row warning条件は0件だった。最大は[7095 Macbee Planet](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A7095)で、参考価格1,160.0713円、raw close 1,162円、FV-gap差-0.369370pt、整数%表示は123%から122%へ変わるが事前基準1.0ptに達しない。[3836 アバントグループ](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A3836)は参考価格1,205.2358円、raw close 1,204円、FV-gap差+0.126424ptで、整数表示は変わらなかった。

全eligibleで唯一の符号反転は[2198 アイ・ケイ・ケイホールディングス](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A2198)だった。参考価格751.0816円でFV gap -0.032220%、raw close 750円で+0.111947%と、いずれも0%の周辺0.15pt以内の表示境界跨ぎである。audit top-20には含まれず、row warningの事前条件に該当しない。

主な例外は次の通りである。

| ticker | scope / impact | reference / raw | abs price error | FV-gap reference / raw | abs delta |
| --- | --- | ---: | ---: | ---: | ---: |
| [5572 Ridge-i](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A5572) | confirm最大price error | 2,757.6170 / 2,747円 | 0.386494% | -41.692940% / -41.467586% | 0.225353pt |
| [3903 gumi](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A3903) | confirm最大FV-gap delta | 241.2007 / 242円 | 0.330289% | 66.873479% / 66.322314% | 0.551165pt |
| [2198 アイ・ケイ・ケイHD](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A2198) | 唯一の符号反転（top-20外） | 751.0816 / 750円 | 0.144213% | -0.032220% / 0.111947% | 0.144167pt |
| [7095 Macbee Planet](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A7095) | current top-20最大 | 1,160.0713 / 1,162円 | 0.165981% | 122.537701% / 122.168330% | 0.369370pt |

### 5.3 AP-02独立検算

SQLiteのexact-date rowとcandidateのmarket cap / shares / FV anchorを別々に抽出し、scratchと別の`awk`式で再計算した。referenceは現行contractどおり先4桁小数へ丸め、その値をpercentage計算に使った。

| ticker | independent inputs | independent result | scratchとの差 |
| --- | --- | --- | ---: |
| [5572 Ridge-i](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A5572) | market cap 119億円、shares 4,315,320、raw 2,747円、FV 1,607.8854円 | reference 2,757.6170円、price error +0.386494357481%、FV-gap delta +0.225353497087pt | 価格0.0000円、percentage < 0.000001 |
| [1964 中外炉工業](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A1964) | market cap 312億円、shares 7,800,000、raw 4,000円、FV 3,342.5円 | reference 4,000.0000円、price error 0%、FV-gap delta 0pt | 価格0.0000円、percentage 0 |

FV-gap差が最大の[3903 gumi](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A3903)も追加spot checkし、reference 241.2007円、price error -0.330289256198%、FV-gap delta -0.551165173318ptでscratchと一致した。

## 6. 判定（5行以内）

- **判定: 現状維持。** exact comparable coverage 98.5763%で、事前の判定条件を満たした。
- FV-gap差のp95は0.093253ptで追加表示基準1.0ptを大きく下回り、0.55%を超えるbasis defectも0件だった。
- current audit top-20にwarning条件はなく、referenceはrank inputではないためproduction rankへの影響はcontract上0件だった。
- raw closeのdual displayもrow warningも追加せず、renderer変更のfollow-up Issueは起票しない。

## 7. 限界と再評価trigger

- 本計測はcurrent cross-sectionと保存済みoperation artifactの表示入力誤差を対象とし、時系列regime、実現return、上場廃止を評価しない。
- master snapshotは2026-05-13の1点のみで、直近12週の当時audit poolをpoint-in-timeに復元できない。current masterを過去日に当てる擬似replayは採否根拠にしない。
- current cross-sectionは反復する週次観測ではないが、対象は決定論的な整数億円round-trip誤差である。数学上限と全ranking-eligible横断のtailを12週top-20 samplingより優先する。
- `現状維持`後にcurrent audit top-20で本reportのwarning条件を満たす実例、またはreference price算出contractの変更を観測した場合、実例と新しいcontractを入力に別Issueで再計測する。
