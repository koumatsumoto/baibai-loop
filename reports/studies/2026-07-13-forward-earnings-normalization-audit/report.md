# forward earnings normalization 監査（#380）

## 0. 目的と判定の位置づけ

目的は、forward PER を優先する機械 E[r] が一時的な利益要因を恒常利益として取り込み、
人間 review 候補の上位を歪めているという懸念を、現行出力と既存の design / confirm 計測へ
分解して確認することである。

本監査は 2026-07-10 as-of の local-only 出力を見た後に行う post-hoc な現状計測である。
また、「一時利益」という銘柄別ラベルは issue の spot check で提示された懸念であり、本監査では
会社 IR を再検証していない。したがって、PER 差を earnings quality の事実とはみなさず、この
report だけで production の metric、gate、E[r] policy を変更しない。

確認する仮説は次の 3 点である。

1. trailing PER / forward PER が大きい銘柄が audit pool 上位に存在する。
2. その差が大きいことだけで、機械 E[r] 上位の主因や一時利益を識別できる。
3. forward PER を外す、または単純な ratio gate を追加する効果が既存較正で支持される。

## 1. 現行 contract

[`estimates.py`](../../../engine/src/baibai_engine/screening/estimates.py) は資産 anchor の PBR と収益 anchor を
blend し、収益 anchor は `per_forward` を優先して欠損時だけ `per_trailing` へ fallback する。
implied upside は ±50% で clip し、その 10% を年率 reversion とするため、reversion 寄与の上限は
**+5.00pt / 年**である。E[r] はこれに予想 DPS 優先の dividend yield と、±5% で clip した
buyback yield を carry として加える。

`accruals_to_assets` は既に derived metric と較正軸に存在するが、現行の selection gate や
E[r] anchor には使わない。したがって、本監査は「earnings quality metric が完全に存在しない」
という前提ではなく、既存 metric が今回の懸念を識別できるかも確認する。

## 2. 入力と provenance

| 入力 | as-of / generated | 管理区分 | 用途 |
| --- | --- | --- | --- |
| `.cache/opportunity/2026-07-10-attempt-2/selection-output.yaml` | as-of 2026-07-10 | local-only / gitignore 対象 | audit pool 順位と E[r] |
| `.cache/opportunity/2026-07-10-attempt-2/candidates.yaml` | as-of 2026-07-10 / run at 2026-07-12 20:54 JST | local-only / gitignore 対象 | PER、E[r] 成分、derived metric |
| [`2026-07-04-preregistered-ranking-validation.md`](../2026-07-04-preregistered-ranking-validation/report.md) | design / confirm 6m | Git 管理の既存計測 | forward / trailing PER 軸比較 |
| [`estimates.py`](../../../engine/src/baibai_engine/screening/estimates.py) | base commit `4fe6f180` | Git 管理 | 現行 E[r] contract |

検算時の SHA-256 は以下である。

- selection output: `3f2327f05fe649ccad7190750c6a2036e3837f921bc4108f4f53e6dadd875208`
- candidates: `ba1359fe3bca102cae61726b86828ee69a2505de9aa73f7a6ebf85289776bd50`
- existing design / confirm report: `3583feae8ffb5e715cdaf6134a43b7e9e3a20722138a83d3f37e83f37cd0eb47`

2 つの YAML artifact は Git history に残らず、同じ raw input から将来再生成できることも保証しない。
この report は local diagnostic の集計結果と限界を固定するが、raw artifact を canonical な会社事実や
長期 evidence として扱わない。

## 3. 再計算方法

AP-02 の検算として、YAML を `baibai_loop.foundation.yaml_io.safe_load` で読み、audit pool の ticker を
candidates へ join した。PER proxy は次で定義した。

```text
forward_earnings_proxy = per_trailing / per_forward
eligible = per_trailing > 0 and per_forward > 0
```

proxy が 1 より大きいとき、forward EPS が trailing EPS より大きいことは示すが、増益、回復、会計上の
一時要因のどれによるかは識別しない。E[r] 寄与率は
`component / er_annual * 100` で再計算した。入力は小数第 4 位で丸められているため、成分和と
`er_annual` には最大 0.01pt の表示丸め差がある。

## 4. 結果

### 4.1 懸念 2 銘柄の E[r] 寄与

| audit rank | ticker | trailing / forward PER | E[r] | reversion | carry | reversion / E[r] | `accruals_to_assets` |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | [4849](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A4849) | 18.31 / 8.77 = **2.09 倍** | 10.62% | 5.00pt | 5.62pt | `5.00 / 10.62 = 47.1%` | −0.46% |
| 3 | [4887](https://jp.tradingview.com/chart/fJupN99c/?symbol=TSE%3A4887) | 18.51 / 10.39 = **1.78 倍** | 10.02% | 1.68pt | 8.35pt | `1.68 / 10.02 = 16.8%` | +0.84% |

4849 は reversion が +5.00pt cap に達しており、forward PER を含む valuation blend の寄与は大きい。
ただし anchor は PBR との blend であるため、この 5.00pt 全量を forward EPS 単独の寄与とは分離できない。
4887 は E[r] の約 83.3%（`8.35 / 10.02`）が carry で、forward PER を使う reversion は約 16.8%である。
carry の内訳は、4849 が dividend 5.62pt + buyback 0.00pt、4887 が dividend 3.35pt +
buyback 5.00pt である。4887 の `net_share_change_yoy: -12.16%` は現行 contract の +5.00pt cap まで
carry に入る。
したがって、2 銘柄とも forward EPS が E[r] 上位を「占有した」という同一原因では説明できない。

既存 `accruals_to_assets` は両銘柄とも 0 近傍であり、この 2 件の concern label を強い異常として
分離しない。一方、この 2 点だけから新しい threshold を選ぶと post-hoc な過適合になる。

### 4.2 audit pool top 20 の proxy 分布

| 集計 | 件数 / 値 | 検算 |
| --- | ---: | --- |
| audit pool | 20 | E[r] 降順 top 20 |
| 両 PER が正値 | 19 | coverage `19 / 20 = 95.0%` |
| proxy 中央値 | 1.03 倍 | 19 件の中央値 |
| proxy 1.5 倍以上 | 6 | `6 / 19 = 31.6%` |
| proxy 2.0 倍以上 | 5 | `5 / 19 = 26.3%` |

proxy 2 倍以上が 5 件ある一方、issue で人手 concern label が付いたのは 2 件である。逆に 4887 は
2 倍未満でも concern label に含まれる。この分布は「ratio が高い銘柄を review する」ための
観察値にはなるが、ratio 単独で一時利益を識別する precision / recall を示さない。

### 4.3 既存 design / confirm 比較

既存の事前登録検証に記録された 6m mean rank IC は次のとおりである。

| 軸 | design | confirm | 窓間方向 |
| --- | ---: | ---: | --- |
| `per_forward` | 0.213 | 0.207 | 両方で正 |
| `per_trailing` | 0.194 | 0.201 | 両方で正 |

両軸の差は小さいが、forward PER が design / confirm の両方で trailing PER を上回る。これは
earnings normalization 問題が無いことを証明しない一方、forward PER を一律に外す効果も支持しない。
また、現行の production decision authority が要求する 3y / 5y evidence は membership、delisting、
corporate-action coverage の不足で blocked であり、この 2 銘柄を見た後に anchor policy を変える
根拠にはできない。

## 5. 効果判定と non-adoption

**懸念の一部は確認したが、metric / gate の production 変更は非採用**とする。

- 4849 では forward PER を含む reversion が cap に達し、normalization を人間が確認する価値がある。
- 4887 の上位要因は主として carry であり、forward EPS 問題だけを直しても同じ現象は解消しない。
- trailing / forward PER ratio は増益・回復・一時要因を区別せず、人手ラベル 2 件から threshold の
  有効性を評価できない。
- 既存 `accruals_to_assets` もこの 2 件を分離しないが、単一 as-of を見た後の新 metric / gate 採用は
  [`改善ループ`](../../../docs/reference/estimate-calibration.md#事前登録と-designconfirm) の事前登録と design / confirm 規律を満たさない。
- 既存較正では forward PER 軸を外すより維持する方向であり、長期 authority は blocked である。

したがって、`per_forward` 優先、reversion cap、selection rules、`accruals_to_assets` の扱いは変更しない。
問題なしという判定ではなく、**単純な proxy/gate では狙った earnings normalization を達成する効果が
立証されていない**という判定である。

## 6. OP3 定性確認への示唆と再評価 trigger

今回の監査からは、OP3 の人間 review で次を定性的に確認する余地がある。

- trailing / forward PER の差が大きい場合、会社の一次 IR で recurring earnings と一時要因を分ける。
- E[r] を reversion / dividend / buyback に分解し、どの成分が順位を押し上げたかを先に確認する。
- 一時的な特別配当や buyback が carry を膨らませる可能性も、forward EPS と同じ normalization 問題として扱う。

ただし、本 PR では OP3 の必須 contract を変更しない。次のいずれかが成立した場合に別 issue で
仮説・ラベル定義・採否基準を結果を見る前に固定し、metric または定性 checklist の変更を再評価する。

- 月次の audit pool で、一次 IR により確認した同じ failure mode が複数 as-of に反復する。
- 一次 IR を根拠に、temporary / recurring を判定した十分な人手ラベル集合を作れる。
- 3y / 5y の production decision evidence が eligible になり、候補 metric の coverage、rank IC、
  top-5 / top-10 median excess、trap 非悪化を design / confirm の両方で比較できる。

## 7. 限界

- 1 as-of、audit pool 20 件だけの post-hoc 診断で、regime をまたぐ頻度や実現 return を示さない。
- concern label 2 件について会社 IR を再検証しておらず、一時利益を canonical fact として確定しない。
- PER ratio は EPS の差を示す proxy であり、増益、回復、会計上の一時要因を識別しない。
- E[r] の reversion は PBR と収益 anchor の blend かつ cap 適用後で、forward PER 単独の限界寄与を
  local artifact から再構成していない。
- local-only artifact の raw rows は Git history に残らず、将来の再生成で同値になる保証はない。
- 既存 6m design / confirm は長期 production authority ではない。有意性、統計的優位、track record は
  主張しない。
