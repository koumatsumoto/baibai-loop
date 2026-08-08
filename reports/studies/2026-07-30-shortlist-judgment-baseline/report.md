# OP3 判断コホートの baseline — 選定 / 非選定 / 機械上位の突き合わせ（#667）

価値tier: T1 — research 枠の配分を決める最上流の判断（OP3）に計測経路を与え、選定の系統誤差を特定できる状態にする。

改善ループ §0「現状計測の確認」にあたる観測記録。**この計測で選定手順・深度契約・screening rules は変更しない**。

## 0. 判定

```yaml
cohort_count: 3
judgment_count: 60
resolved_return_cohorts: 0
first_resolvable_date: 2026-10-17
drawdown_observed_cohorts: 2
```

判断は 60 件蓄積しているが、**リターンで採点できる cohort はまだ 0 件**である。最も古い shortlist が 2026-07-17 なので、最短 horizon（3m）の target は 2026-10-17 で、採点日はその日以前の最終取引日になる。この report は方法を固定し、最初の採点可能日を明示するためのものである。

ドローダウンは部分窓でも観測できるので、そこは今日から数値が出る（§4）。

## 1. 再現手順

```bash
uv run baibai-engine screening shortlist outcome --horizon 3m --out /tmp/shortlist-outcome.yaml
```

観測日 2026-07-30。application DB の published shortlist 全件を対象にする。`stores/market/market.sqlite` の最終 bar は 2026-07-29。

## 2. 母集団と 3 コホート

| 概念 | 定義 |
| --- | --- |
| 母集団 | その shortlist の entry 全件（selected + rejected）。人間がレビューした pool そのもの |
| benchmark | 母集団の forward return 中央値 |
| selected | OP3 が research 枠を与えた銘柄 |
| rejected | 同じ pool で見送った銘柄 |
| machine top-N | 同じ pool を機械 E[r] 降順に並べ、selected と同数だけ取ったもの |

machine cohort を selected と同数にするのは、両者が同じ問いに同じ規模で答えるようにするためである（`machine_matches_selected_size` が同数であることを payload で示す）。

**機械 E[r] は判断側へ焼き込む**。run store は数世代しか保持せず、日次バッチが毎営業日 prune するので、3m horizon が満期を迎える頃には束縛 run は消えている。判断時点の順位を判断と一緒に残さない限り、「機械順位への付加価値」は原理的に測れない。`shortlist publish` が bound run の `metrics.er_annual` を各 entry へ書き込み、計測はそれを読む。

## 3. 現在の coverage（実際の payload）

| shortlist | status | machine_basis | unresolved | 内訳 | unpriced_exit | adjustment_factor |
| --- | --- | --- | ---: | --- | ---: | --- |
| 2026-07-29 event-window-value | unresolved | `judgment_estimate` | 20 | `unresolved_future_horizon: 20` | 0 | complete |
| 2026-07-28 carry-durability | unresolved | `estimate_missing` | 20 | `unresolved_future_horizon: 20` | 0 | complete |
| 2026-07-17 riskoff-value | unresolved | `estimate_missing` | 20 | `unresolved_future_horizon: 20` | 0 | complete |

- 全 60 観測が `unresolved_future_horizon`、すなわち **満期前**である。値付けの失敗でも上場廃止でもない。
- `estimate_missing` の 2 件は焼き込み前に publish された shortlist で、束縛 run が既に prune されている。2026-07-29 の shortlist は束縛 run が生存しているため fallback で estimate を得ている（`judgment_estimate`）。**焼き込み後に publish される shortlist は run の寿命に依存しない。**
- `unpriced_exit` は 0。窓中に市場から消えた銘柄はまだ無い。買収・上場廃止は selected 側に起きやすい良い結末なので、この件数は今後も毎回確認する。

## 4. 現時点で観測できるもの（永久損失カテゴリ別ドローダウン）

選定時の `ploss` 判定と、選定日から**観測できた最終取引日**までの最大下落（終値ベース、分割調整済み、下落しなければ 0）。

| shortlist | 観測窓端 | ploss | n | 中央 DD | 最悪 DD |
| --- | --- | --- | ---: | ---: | ---: |
| 2026-07-17 riskoff-value | 2026-07-29 | 低 | 1 | −0.2% | −0.2% |
| | | 中低 | 5 | −2.0% | −3.0% |
| | | 中 | 2 | −0.6% | −1.3% |
| 2026-07-28 carry-durability | 2026-07-29 | 中低 | 4 | 0.0% | 0.0% |
| | | 中 | 3 | 0.0% | 0.0% |
| | | 要精査 | 1 | 0.0% | 0.0% |

2026-07-29 の shortlist は選定日の翌取引日がまだ無いので窓が成立せず、ドローダウンを出さない。

窓が 1〜9 営業日しかないので、**この表から ploss 判定の当否は読めない**。順序（低 < 中低 < 中 < 要精査 < 高）が実現下落の序列と一致するかは、窓が数か月に伸びてから見る。ここで固定するのは計測の形と初期値である。

## 5. 検算（AP-02）

- **judgment_count**: 3 shortlist × 20 entry = 60。各 shortlist は selected 8 + rejected 12 で、payload の `pool_size` も 3 件すべて 20。
- **first_resolvable_date**: 最古 shortlist 2026-07-17 + 3 か月 = 2026-10-17。`require_horizon("3m").target_date` は暦月加算のうえ target 以下の最終取引日へ解決するので、採点日は 2026-10-17 以前の最終取引日になる。
- **ドローダウンの符号**: 下落した銘柄だけが負値になり、上昇した銘柄は 0 に丸められる。2026-07-28 cohort が全件 0.0% なのは、選定翌日の 1 営業日で終値が選定日を下回った銘柄が無いことを意味する（下落幅 0 の観測であって、未計測ではない）。
- **観測窓端**: payload の `drawdown_window_end` は要求端（今日）ではなく bar store の最終取引日 2026-07-29。要求端をそのまま書くと、store が止まった期間の分だけ「下落なし」を実際より長い窓の結論として提示してしまう。

## 6. 誠実性の限定（毎回併記する）

- **非ランダム割当**: selected と rejected の割当は E[r]・耐性・開示スキャンと相関しており、無作為化されていない。したがってこれは因果効果ではなく記述比較である。payload も `comparison_basis: descriptive_non_random_assignment` を持つ。
- **自己参照**: benchmark は母集団の中央値で、selected はその母集団の一部（20 件中 8 件）である。selected の超過は「pool 全体に対する相対」であって独立母集団に対する超過ではない。selected が pool の 4 割を占めるので、構成上、超過の絶対値は独立 benchmark に対するそれより小さく出る。
- **窓の重複と少数標本**: cohort の forward 窓は重なり、独立でない。3 cohort・60 判断では効果量も勝率も主張しない。
- **price-only**: 配当を含まない。較正リプレイと同じ basis（`metric_basis: price_return_only`）。
- **ploss の range restriction**: selected の ploss は「低 / 中低 / 中 / 要精査」に偏り、「高」は選定されないので観測されない。判別できるのは選定された範囲内の序列だけで、スケール全体の較正ではない。

## 7. 採否基準の事前登録

この計測を根拠に OP3 の手順（深度契約・narrative 規約）を変える場合の基準を、計測より先にここへ固定する。

- **3m / 6m は alert のみ**。手順変更の根拠にしない（短期 horizon の成績最適化を目的にしないという doctrine 柱 5 の規律による）。
- **手順変更の検討に進む条件**: 1y 以上の horizon で、cohort 数 8 以上・selected の中央超過が rejected の中央超過を下回る状態が、時間で 2 分割した両期間で同方向に出ること。片側のみは「不確定」とする。
- **基準を後から動かさない**。動かす場合は、動かしたことと理由を次の report に明記する。

## 8. 監視事項

- **2026-10-17 以前の最終取引日**に最初の 3m cohort（2026-07-17 shortlist）が満期を迎える。その時点で再実行し、selected / rejected / machine の中央超過と coverage 内訳を記録する。
- `machine_basis` が `estimate_missing` の 2 cohort は焼き込み前の publish なので恒久的に機械比較ができない。焼き込み後の shortlist が 8 件たまるまで machine 比較の母数は増えない。
- `unpriced_exit` の件数を毎回確認する。窓が伸びるほど買収・上場廃止が入り、selected 側に偏って母集団から消える経路になる。
- ドローダウン表は窓が伸びるたびに更新し、ploss の順序と実現下落の序列を突き合わせる。
