# ADV 1 億円/日 selection floor 監査（#399）

## 0. 目的と判定の位置づけ

目的は、現行の `selection.liquidity.min_avg_turnover_oku: 1.0`（20 日平均売買代金
1 億円/日）が、他の selection gate を通る候補をどれだけ ranking から外しているかを測り、
ADV floor を下げる正式検証へ進む効果があるかを判定することである。

本監査は、既知の 2026-07-08 all-common-stock 出力を見た後に 0.5 / 0.3 億円の閾値を比較する
**post-hoc な固定 E[r] counterfactual** である。事前登録した design / confirm 検証ではなく、
閾値選択の out-of-sample 性は消費済みである。したがって、本 report の数値だけで production
rule を変更しない。

結論は次のとおりである。

- 0.5 億円 floor は固定 E[r] ranking の eligible を 1,473 件から 1,767 件へ 294 件増やし、
  追加候補が 1 位、top 10 に 3 件、top 50 に 15 件流入する。
- 0.3 億円 floor は baseline 比 492 件を追加し、top 10 に 6 件流入する。ただし、0.5 億円から
  追加で下げる部分の最高順位は 6 位である。比較した variant のうち、baseline に最も近く
  material だった 0.5 億円を正式検証の対象とする。
- 単一 as-of の shortlist に明確な差が出るため formal validation の価値はあるが、production
  採用効果は未立証である。0.5 億円だけを事前登録した rules variant として design / confirm
  検証する follow-up を推奨する。

## 1. 現行 contract

本番 rules は
[`selection.liquidity.min_avg_turnover_oku: 1.0`](../method/screening-rules/2026-07-06T000000+0900.yaml)
を持つ。[`SelectionLiquidityRules.matches`](../src/baibai_engine/screening/rule_config.py) は、candidate の
`avg_turnover_oku` がこの値未満なら ranking 母集団から除外する。欠損 liquidity fact、
`market_cap_oku < 100`、上場期間 182 日未満、required JPX flag、`metrics.er_annual` 欠損も独立に
ranking 対象外になる。

selection と比較中央値の母集団は同じ liquidity predicate を使うため、ADV floor の production
変更は候補 filter だけではない。[`liquid_median_population`](../src/baibai_engine/screening/universe.py)
が変わり、sector / market median、FV anchor、E[r] も再計算される。本監査はその再構築を行わず、
保存済み E[r] を固定した一次近似だけを扱う。

## 2. 入力と provenance

| 入力 | as-of / generated | SHA-256 | 用途 |
| --- | --- | --- | --- |
| `reports/2026-07-13-small-cap-selection-floor-audit.md` | report 2026-07-13 | `31c79d2fcd99afebc81087e52a1ba3e6817c407fb4cd1eb5fac285a737169104` | #382 の方法・限界との整合 |
| `.cache/280-current-candidates-2026-07-08.yaml` | as-of 2026-07-08 / generated 2026-07-10 23:17 JST | `dd0f10486f402b5c1b311a842ef0dc5d24367e45638f7d1e681c7bb2ae52ff90` | all-common-stock 3,744 件の liquidity fact と固定 E[r] |
| `records/_config/screening-rules/2026-07-06T000000+0900.yaml` | current rules | `bd4bba8ceb74d08fefb4b670ddf28f2e19b8368679678237599be453f214c103` | market cap・ADV・seasoning・JPX flag contract |

candidate artifact の `run_id` は `screening-20260708`、`universe_size` は 3,744、scope は
`all-common-stocks`、markets は `prime/standard/growth` である。この artifact は local-only であり、
raw input 自体は Git history に固定されないため、hash・式・集計値を本 report に残す。

計算時の repository HEAD は `9a6edc3bdfa693a1576d45348d99feb46c9c5dbb` である。判定は path 名や
working tree の状態ではなく、上記 3 入力の hash に結びつける。

## 3. 再計算方法

### 3.1 集合と順位

ADV floor を `f ∈ {1.0, 0.5, 0.3}` 億円/日として、candidate `c` の固定 E[r] eligible を次で
定義した。

```text
eligible(c, f) = market_cap_oku is not null and market_cap_oku >= 100
                 and avg_turnover_oku is not null and avg_turnover_oku >= f
                 and listing_span_days is not null and listing_span_days >= 182
                 and jpx_flags is not null
                 and jpx_flags has no required exclusion flag
                 and metrics.er_annual is not null
```

required exclusion flag は現 rules の `特別注意銘柄 / 整理銘柄 / 取引停止 / 上場廃止警告` である。
各 floor の集合を `E_f`、現行 1.0 億円との差分を `A_f = E_f - E_1.0` とした。0.3 億円の
段階差分は `S_0.3 = E_0.3 - E_0.5` でも別に数えた。

順位は各 `E_f` について、保存済み `metrics.er_annual` 降順、同値なら ticker 昇順で再構築した。
top-N 流入は次式で機械集計した。

```text
inflow(f, N) = count(ticker in A_f where fixed_er_rank_f(ticker) <= N)
```

これは #382 と同じ固定 E[r] 近似であり、median / FV / E[r] を作り直す正式な selection replay
ではない。evidence hit の有無は ranking gate ではないため条件に加えていない。

### 3.2 AP-02 検算

YAML を `baibai_loop.foundation.yaml_io.safe_load` で読み、上記式を Python で再計算した。さらに
独立チェックとして、全 3,744 件を `candidate_record_from_mapping` へ通し、rules の
`SelectionLiquidityRules.matches(require_facts=True)` を `model_copy` した 3 floor で再実行した。
両経路の件数は一致した。

ADV 以外の gate と E[r] availability を通る母集団 2,341 件は、保存済み ADV により次の排他的な
帯へ分解できる。

```text
ADV >= 1.0       : 1,473
0.5 <= ADV < 1.0:   294
0.3 <= ADV < 0.5:   198
ADV < 0.3       :   376
合計            : 2,341  (1,473 + 294 + 198 + 376 = 2,341)
```

floor ごとの liquidity 通過数は 1,475 / 1,769 / 1,967 で、いずれも E[r] 欠損 2 件を引くと
ranking eligible 1,473 / 1,767 / 1,965 になる。

件数差・比率も次のとおり再計算した。

- 0.5 億円: `1,767 - 1,473 = 294`、`294 / 1,473 * 100 = 19.9593%` → baseline 比 **20.0% 増**
- 0.3 億円: `1,965 - 1,473 = 492`、`492 / 1,473 * 100 = 33.4012%` → baseline 比 **33.4% 増**
- 0.5 → 0.3 の追加: `1,965 - 1,767 = 198`、`198 / 1,767 * 100 = 11.2054%` → **11.2% 増**

単位検算では、0.5 億円は 5,000 万円/日、0.3 億円は 3,000 万円/日である。本監査時点の
planning baseline だった 20〜30 万円の単発注文を単純に日次 ADV で割ると、0.5 億円で
`20〜30万 / 5,000万 * 100 = 0.4〜0.6%`、0.3 億円で
`20〜30万 / 3,000万 * 100 = 0.6667〜1.0%` になる。この注文額は 2026-07-14 の再現入力であり、
将来の policy literal ではない。現在の planning baseline は
[`portfolio-management.md#capital-guidance`](../docs/portfolio-management.md#capital-guidance)を正本とする。
また、この比率は執行可能性や market impact を示すものではない。

## 4. 結果

### 4.1 floor 別の eligible と top-N 流入

`追加` と top-N 流入は現行 1.0 億円 floor 比である。

| ADV floor（億円/日） | ranking eligible | 現行比追加 | 現行比増加率 | 追加候補の最高順位 | top 5 / 10 / 20 / 50 流入 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1.0（現行） | 1,473 | 0 | 0.0% | — | 0 / 0 / 0 / 0 |
| 0.5 | 1,767 | 294 | 20.0% | **1** | **1 / 3 / 5 / 15** |
| 0.3 | 1,965 | 492 | 33.4% | **1** | **1 / 6 / 7 / 17** |

0.3 億円のうち、0.5 億円では入らず 0.3 億円で初めて入る 198 件だけを見ると、最高順位は 6 位、
top 5 / 10 / 20 / 50 流入は `0 / 3 / 3 / 6` である。

### 4.2 上位へ流入する限界候補

0.5 億円 variant で top 10 へ流入する 3 件は次のとおりである。

| rank | ticker | E[r] annual | ADV（億円/日） | market cap（億円） |
| ---: | --- | ---: | ---: | ---: |
| 1 | [5729 日本精鉱](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A5729) | 24.28% | 0.5 | 150 |
| 6 | [2491 バリューコマース](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A2491) | 15.72% | 0.7 | 158 |
| 7 | [9658 ビジネスブレイン太田昭和](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A9658) | 15.33% | 0.6 | 361 |

0.3 億円へさらに下げた段階で新たに top 10 へ入るのは次の 3 件である。

| rank | ticker | E[r] annual | ADV（億円/日） | market cap（億円） |
| ---: | --- | ---: | ---: | ---: |
| 6 | [7305 新家工業](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A7305) | 16.74% | 0.4 | 267 |
| 8 | [7463 アドヴァングループ](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A7463) | 15.63% | 0.3 | 398 |
| 10 | [2003 日東富士製粉](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A2003) | 15.00% | 0.3 | 665 |

現行 1.0 億円の top 1 は E[r] 20.35% だったが、0.5 億円 variant では固定 E[r] 24.28% の
[5729 日本精鉱](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A5729)が 1 位になる。したがって、
ADV floor は、market cap 100 億円以上を維持したこの単一 as-of の research queue に対して
material な binding constraint である。小型株全体や market cap 100 億円未満の不可視性は
この監査から判定しない。

## 5. 効果判定と production non-change

0.5 億円への緩和は、固定 E[r] 上位 5 / 10 / 20 / 50 のすべてを変える。#382 の market cap floor
単独緩和では追加候補の最高順位が 96 位、top 50 流入 0 件だったのに対し、本監査では最高 1 位、
top 50 流入 15 件である。したがって、ADV floor を正式な基盤改善仮説として検証する効果がある。

一方、**production の `min_avg_turnover_oku: 1.0` は本監査では変更しない**。理由は次のとおりである。

1. 既知の 1 as-of を見た後の post-hoc 比較であり、将来または別 regime で同じ差が再現するか未検証。
2. 保存済み E[r] を固定しており、floor 変更後の比較中央値、FV anchor、E[r] を再構築していない。
3. ranking 上位への流入は期待リターンの実現、value trap 非悪化、執行可能性を証明しない。
4. production decision に必要な 3y / 5y membership、delisting、corporate-action coverage は現 provider
   では不足し、[`estimate-calibration`](../docs/reference/estimate-calibration.md) 上の evidence は
   blocked である。

次の改善単位は、0.5 億円だけを動かす rules variant を結果計測前に事前登録し、現行 panel と分離して
design / confirm を評価することである。0.3 億円はこの report で結果を見た後の追加 variant であり、
同じ検証で grid search しない。比較した variant のうち baseline に最も近く material だった
0.5 億円を単独検証する。

## 6. formal follow-up の着手条件と判定対象

follow-up は次を満たすときに着手する。

- 3y / 5y の membership、delisting、corporate-action coverage が production decision evidence として
  eligible になる。
- exact design / confirm 期間と採否基準を、variant panel 構築・結果計測より前に tracked file へ
  commit する。
- variant は `min_avg_turnover_oku: 0.5` だけを変更し、別 calibration store・別 rules hash で
  `--force` 再構築する。

判定対象は design / confirm 双方の `er_calibration`、
`summary.selection.recommended_rank_top5/10.mean_median_excess`、
`summary.selection.recommended_rank_top10.mean_trap_rate`、coverage / integrity、
および現 as-of の `run` / `select` による shortlist 差分とする。両期間・3y / 5y で事前基準を通過した
場合だけ production 採用候補とし、片側だけなら不確定、逆方向または integrity block なら非採用とする。

## 7. 限界

- 2026-07-08 の 1 as-of に対する post-hoc 監査であり、有意性、統計的優位、track record を示さない。
- `.cache/280-current-candidates-2026-07-08.yaml` は local-only で、raw artifact 自体は Git 管理されない。
- 固定 E[r] counterfactual であり、median / FV / E[r] の再構築を行っていない。
- ADV は candidate artifact 上で 0.1 億円単位へ丸められており、閾値近傍の連続的な感度を測れない。
- market cap floor 100 億円は維持したため、100 億円未満の micro-cap を ranking に戻す joint variant
  ではない。本結果だけで「小型株全体の不可視性を解消する」とは言えない。
- 20 日平均売買代金だけでは、spread、order book depth、約定偏り、寄付・引けの流動性、個別注文の
  market impact を評価できない。本監査ではいずれも未評価である。
- 低 ADV 候補の開示品質、上場廃止率、急変時の exit liquidity、portfolio sizing は評価していない。
- 上位へ流入した個別候補の一次IR、earnings quality、corporate action は未確認であり、保存済み
  E[r] の推定誤差や一時利益の影響をそのまま引き継ぐ。
