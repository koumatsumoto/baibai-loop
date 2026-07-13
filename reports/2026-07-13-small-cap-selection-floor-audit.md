# 時価総額 100 億円 selection floor 監査（#382）

## 0. 目的と判定の位置づけ

目的は、`selection.liquidity.min_market_cap_oku: 100` が小型株を hard exclude する事実と、
floor を warning へ緩和することで現在の選定品質が改善する見込みを分けて測ることである。
本監査は、既知の 2026-07-08 出力を使う post-hoc な現状計測であり、事前登録した
design / confirm 検証ではない。したがって、本 report の数値だけで production rule を変更しない。

確認する仮説は次の 2 点である。

1. 100 億円未満という単独条件だけを見ると、候補出力の大きな割合が selection 対象外になる。
2. ただし、売買代金・上場期間・JPX flag・E[r] availability を同時に適用すると、floor 緩和で
   実際に ranking へ加わる限界候補は大幅に減り、現在の上位候補は変わらない。

## 1. 現行 contract

本番 rules は [`selection.liquidity.min_market_cap_oku: 100`](../records/_config/screening-rules/2026-07-06T000000+0900.yaml)
を持ち、[`SelectionLiquidityRules.matches`](../src/baibai_loop/screening/rule_config.py) が
`market_cap_oku < 100` を不通過にする。selection と比較母集団は同じ liquidity predicate を使う。
[`liquid_median_population`](../src/baibai_loop/screening/universe.py) はこの母集団を sector / market
median の計算へ渡すため、floor の変更は候補の追加だけでなく、全銘柄の相対 valuation と E[r]
anchor を再計算する変更になる。

## 2. 入力と provenance

| 入力 | as-of / generated | 管理区分 | 用途 |
| --- | --- | --- | --- |
| `records/_config/screening-rules/2026-07-06T000000+0900.yaml` | rules file | Git 管理 | 現行の liquidity contract |
| `records/02-candidates/2026/07/2026-07-08.yaml` | as-of 2026-07-08 / 2026-07-09 01:59 JST | gitignore 対象の operational output | 旧 evidence-hit 集合に対する 691 / 1,585 の再計算 |
| `.cache/280-current-candidates-2026-07-08.yaml` | as-of 2026-07-08 / 2026-07-10 23:17 JST | local-only diagnostic | all-common-stock 出力での限界候補と固定 E[r] 順位 |

両 candidate artifact の `universe_size` は 3,744、`run_id` は `screening-20260708` である。
旧 operational output は `evidence_hits` が 1 件以上ある 1,610 件だけを保持し、current diagnostic は
同じ 1,610 件に evidence なしを加えた全 3,744 件を保持する。いずれも Git の再現可能な fixture
ではないため、raw artifact を canonical evidence とせず、本 report に集計式・hash・coverage を固定する。

検算時の SHA-256 は以下である。

- operational output: `b070f4aa0a164488a7e428f48cae806bb83bdb57c47c77501ee219514c0210e4`
- current diagnostic: `dd0f10486f402b5c1b311a842ef0dc5d24367e45638f7d1e681c7bb2ae52ff90`
- rules: `bd4bba8ceb74d08fefb4b670ddf28f2e19b8368679678237599be453f214c103`

## 3. 再計算方法

AP-02 の検算として、YAML を repo の `baibai_loop.foundation.yaml_io.safe_load` で読み、次の集合を
機械的に数えた。

```text
market_cap_known = market_cap_oku is not null
under_100_oku = market_cap_known and market_cap_oku < 100
other_gates_pass = under_100_oku
                   and avg_turnover_oku >= 1.0
                   and listing_span_days >= 182
                   and jpx_flags has no required exclusion flag
ranking_eligible = other_gates_pass and metrics.er_annual is not null
```

固定 E[r] counterfactual は、100 億円 floor だけを外した `ranking_eligible` 全件を
`metrics.er_annual` 降順、ticker 昇順で並べた。この計算は current artifact の E[r] を固定するため、
比較中央値まで再構築する正式な rules variant ではない。

## 4. 結果

### 4.1 旧 evidence-hit 出力での 43.6% 再現

| 段階 | 件数 | 検算 |
| --- | ---: | --- |
| 出力全体 | 1,610 | evidence あり 1,610 / 1,610 |
| 時価総額あり | 1,585 | 25 件は market cap 欠損 |
| 100 億円未満 | 691 | `691 / 1,585 * 100 = 43.5962%` → **43.6%** |
| 100 億円未満かつ ADV 1 億円以上 | 26 | 691 件のうち 26 件 |
| さらに上場期間・JPX flag を通過 | 24 | 独立 gate を同時適用 |
| さらに E[r] あり | 24 | 固定 E[r] counterfactual の追加候補 |

691 件は「market cap 単独で除外される件数」であり、floor を外せば 691 件すべてが ranking に
加わることを意味しない。他の liquidity 条件を同時に満たすのは 24 件である。

### 4.2 current all-universe diagnostic

| 段階 | 件数 / 順位 | 検算 |
| --- | ---: | --- |
| 全普通株出力 | 3,744 | all-common-stock scope |
| 時価総額あり | 3,570 | 174 件は market cap 欠損 |
| 100 億円未満 | 1,188 | `1,188 / 3,570 * 100 = 33.2773%` → **33.3%** |
| 100 億円未満かつ ADV 1 億円以上 | 62 | 1,188 件のうち 62 件 |
| さらに上場期間・JPX flag を通過 | 60 | market cap 以外の liquidity 条件を通過 |
| さらに E[r] あり | 60 | 固定 E[r] counterfactual の追加候補 |
| floor 緩和後の E[r] あり母集団 | 1,533 | 現 floor 通過 1,473 + 追加 60 |
| 追加候補の母集団比 | 3.91% | `60 / 1,533 * 100 = 3.9139%` |
| 追加候補の最高順位 | 96 | 固定 E[r] counterfactual |
| top 5 / 10 / 20 / 50 への流入 | 0 / 0 / 0 / 0 | 各 cutoff で機械集計 |

100 億円未満は current 出力の時価総額既知銘柄の 33.3% を占める一方、他 gate と E[r] を通る
限界候補は 60 件であり、固定 E[r] の上位 50 に入る銘柄はない。したがって、単一 as-of の
現 shortlist に対する改善効果は観測されない。

## 5. 効果判定と non-adoption

hard floor が存在するという指摘は正しい。ただし、43.6% は旧 evidence-hit 出力内で market cap
だけを独立に見た比率であり、選定上の機会損失を 691 件とみなすと過大になる。current
all-universe で他条件も通る 60 件を加えても、固定 E[r] では最高 96 位で、運用上の人間 review
候補へ流入しない。

この監査では `min_market_cap_oku` を変更しない。理由は次のとおりである。

1. 現 as-of の top 50 に選定差がなく、直接の shortlist 改善を確認できない。
2. floor は比較母集団にも使われるため、単純な filter 解除ではなく全銘柄の median / E[r] を
   再構築する rules variant が必要である。
3. [`estimate-calibration`](../docs/reference/estimate-calibration.md) が要求する 3y / 5y の
   membership、delisting、corporate-action coverage は現 provider で不足し、production decision
   evidence は blocked である。
4. 既知の 1 as-of を見た後の閾値変更は、[`改善ループ`](../docs/operations/improvement-loop.md) の
   事前登録・design / confirm 規律を満たさない。

判定は「問題なし」ではなく、**構造的な hard gate は確認したが、production 変更の効果は未立証のため
非採用**である。

## 6. 再評価 trigger

次のいずれかが成立した場合、別 issue で rules variant の仮説・採否基準を結果を見る前に固定し、
`min_market_cap_oku` を変更した panel を現行 panel と分離して再構築する。

- 月次の固定 E[r] 診断で、100 億円未満かつ他 gate 通過銘柄が top 20 または人間 review 候補へ入る。
- 100 億円 floor 以外の liquidity contract を変更し、限界候補集合が変わる。
- 3y / 5y の membership、delisting、corporate-action coverage が揃い、production decision evidence
  が eligible になる。

正式な再評価では、design / confirm の両期間について top-5 / top-10 の median excess、trap、
coverage を比較し、現 as-of の `run` / `select` で shortlist 差分を運用テストする。

## 7. 限界

- 2026-07-08 の 1 as-of だけを使うため、regime をまたぐ選定効果や実現 return を示さない。
- current diagnostic は local-only であり、raw artifact 自体は Git history に残らない。
- 固定 E[r] counterfactual は、floor 緩和後の比較中央値・FV anchor・E[r] 再計算を含まない。
- 100 億円未満の銘柄について、売買時の market impact、order book、開示品質、上場廃止率を
  この artifact から評価できない。
- 有意性、統計的優位、track record は主張しない。
