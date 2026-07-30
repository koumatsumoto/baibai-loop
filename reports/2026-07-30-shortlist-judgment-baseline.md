# OP3 判断コホートの baseline — 選定 / 非選定 / 機械上位の突き合わせ（#667）

価値tier: T1 — research 枠の配分を決める最上流の判断（OP3）に計測経路を与え、選定の系統誤差を特定できる状態にする。

改善ループ §0「現状計測の確認」にあたる観測記録。**この計測で選定手順・深度契約・screening rules は変更しない**。

## 0. 判定

```yaml
cohort_count: 3
judgment_count: 60
matured_return_cohorts: 0
first_resolvable_date: 2026-10-17
drawdown_observations: 2
```

判断は 60 件蓄積しているが、**リターンで採点できる cohort はまだ 0 件**である。最も古い shortlist が 2026-07-17 なので、最短 horizon（3m）の target は 2026-10-17。この report は方法を固定し、最初の採点可能日を明示するためのものである。

ドローダウンは部分窓でも観測できるので、そこは今日から数値が出る（§3）。

## 1. 再現手順

```bash
uv run baibai-engine screening shortlist outcome \
  --horizon 3m --horizon 6m --horizon 1y --out /tmp/shortlist-outcome.yaml
```

観測日 2026-07-30。application DB の published shortlist 全件を対象にする。

## 2. 母集団と 3 コホート

| 概念 | 定義 |
| --- | --- |
| 母集団 | その shortlist の entry 全件（selected + rejected）。人間がレビューした pool そのもの |
| benchmark | 母集団の forward return 中央値 |
| selected | OP3 が research 枠を与えた銘柄 |
| rejected | 同じ pool で見送った銘柄 |
| machine top-N | 同じ pool を機械 E[r] 降順に並べ、selected と同数だけ取ったもの |

machine cohort を selected と同数にするのは、両者が同じ問いに同じ規模で答えるようにするためである。E[r] は shortlist が束縛した run から読む。run store は数世代しか保持しないので、**古い shortlist の run が prune 済みなら machine cohort は `unresolved_pruned_run` として計算しない**（別の run の数値で埋めると、判断が見ていない数値と比較することになる）。

実測: 3 shortlist すべてで `machine_basis: unresolved_pruned_run`。束縛された run（`run-revision-61323e5d8f5` ほか）は既に prune されている。selected と rejected の比較は価格だけで成立するのでこの制約を受けない。

## 3. 現時点で観測できるもの（永久損失カテゴリ別ドローダウン）

選定時の `ploss` 判定と、選定日から観測日までの最大下落（終値ベース、分割調整済み、下落しなければ 0）。

| shortlist | 窓 | ploss | n | 中央 DD | 最悪 DD |
| --- | --- | --- | ---: | ---: | ---: |
| 2026-07-17 riskoff-value | 〜07-30 | 低 | 1 | −0.2% | −0.2% |
| | | 中低 | 5 | −2.0% | −3.0% |
| | | 中 | 2 | −0.6% | −1.3% |
| 2026-07-28 carry-durability | 〜07-30 | 中低 | 4 | 0.0% | 0.0% |
| | | 中 | 3 | 0.0% | 0.0% |
| | | 要精査 | 1 | 0.0% | 0.0% |

窓が 2〜13 営業日しかないので、**この表から ploss 判定の当否は読めない**。順序（低 < 中低 < 中 < 要精査 < 高）が実現下落の序列と一致するかは、窓が数か月に伸びてから見る。ここで固定するのは計測の形と初期値である。

## 4. 誠実性の限定（毎回併記する）

- **非ランダム割当**: selected と rejected の割当は E[r]・耐性・開示スキャンと相関しており、無作為化されていない。したがってこれは因果効果ではなく記述比較である。payload も `comparison_basis: descriptive_non_random_assignment` を持つ。
- **窓の重複と少数標本**: cohort の forward 窓は重なり、独立でない。3 cohort・60 判断では効果量も勝率も主張しない。
- **price-only**: 配当を含まない。較正リプレイと同じ basis（`metric_basis: price_return_only`）。
- **machine cohort の欠測**: run prune により現時点では全 cohort で unresolved。今後の shortlist は run が生きているうちに計測すれば解決する。

## 5. 監視事項

- **2026-10-17** に最初の 3m cohort（2026-07-17 shortlist）が満期を迎える。その時点で再実行し、selected / rejected / machine の中央超過を記録する。
- run prune が machine cohort を潰すので、shortlist publish と同じサイクルで本計測を回すか、E[r] を shortlist 側へ保存する変更を検討する（後者は schema 変更なので、prune による欠測が実際に判断を妨げてから起票する）。
- ドローダウン表は窓が伸びるたびに更新し、ploss の順序と実現下落の序列を突き合わせる。
