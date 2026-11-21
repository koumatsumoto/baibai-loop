# 探索索引から調査対象を選ぶ

ScreeningSelectionは「何が価格に対して安い可能性を作るか」を調べるための、保存済みGenerationの索引。候補順位・割安判定・採否ではない。[日次統合](../../jobs/src/baibai_jobs/integration/models.py)が[普通株の探索行](../../engine/src/baibai_engine/machine_analysis/screening/policies/discovery.py)を固定し、[企業判断の入口](../../.agents/skills/investment-judgment/SKILL.md#開始と固定対象)がそれを読む。

## 発行物を固定して読む

1. 通常の新規発見は `investment-judgment selection-context` の `--scope discover_new` から始める。返された `repeat_command_args` でSelection ID・基準日・timezone付きcutoffを固定する。
2. 出力にある問いを `--discovery-question <KEY>` で選ぶ。続きを見るには `next_args`、同じ世代の個社モデルを読むには行の `detail_args`（`--compare-ticker`）を使う。ページ順や経路数は割安順位ではない。
3. 銘柄の指定は `--scope specified_stocks`、保有点検は `--scope review_positions`。明示対照は調査・提案対象とは別に扱う。台帳を読んでいないことを未保有確認済みにしない。保有は現在の台帳を事実時刻で再生するため、過去の約定の後追記で同じcutoffの対象が変わり得る。

新しい発行は候補外も含む普通株全体の探索行を持つ。軽量索引のない公表物は `unindexed_legacy_publication` であり、対象0件や現行の順位ではない。必要な会社だけ同じ公表IDで照合し、現在のモデルから当時の選定を作り直さない。照会はResearchの開始や対象追加ではない。

<a id="candidate-discovery"></a>
## 観測から仮説へ進む

利益が増えた会社なら、その利益が追加投資や買収の資金を差し引いても買い手に残るかを見る。簿価に対して価格が低い会社なら、どの資産が換金でき、誰に帰属するかを確かめる。会社の規模や業種、会計期間、株式の権利が異なる数字だけを比べて結論を出さない。

| 調べたいこと | 使う材料と限界 |
| --- | --- |
| 成長・営業回復、持続的な稼ぎ | 原期・前年・会社予想を分け、利益の符号と実額を確認する。増収から有機成長・投資収益を決めつけない。[開示と会計比較](model-observations.md#financial-periods) |
| 資産・資本異動・買い手の取り分 | 報告簿価は回収額ではなく、大量保有報告は発行会社の自己株確認ではない。原典で効力日・会社・株式basisを揃える。[資本・価格・配当](model-observations.md#capital-basis) |
| 価格差、活動、地合い | 銘柄・指数・実在の同業を分け、同一端点の価格と比較可能な窓を使う。未分類の「その他」は同業ではない。相対超過・調整価格は割安判定や発注価格ではない。[市場・業種の比較](model-observations.md#market-context-reads) |

同じ問いでも測定基準の異なる数値は混ぜない。例えば、対抗候補と同じ経路・variant・価格座標を選んだ `--price-coordinate-min` は閲覧条件であり、要求利回りや棄却基準ではない。値なしの相手を割高とせず、必要なら全母集団や個社へ戻る。[価格・事業座標の算出](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_path_inputs.py)を参照する。

## 入力不足と品質

未取得、モデル未生成、企業への非該当、価格・分母の不足、既知の条件不成立を区別する。

- `input_gap` は、入力や価格の基準が未確定の会社を調べる問い。
- `outside_display` は、発行時の索引で `screening_lane` が `no_current_hypothesis` の会社を調べる問い。過去の画面に載ったかどうかは見ない。

同じ会社が両方に該当することがある。いずれも棄却や割安の判定ではない。[原典・時点・不足の規則](model-observations.md#source-owners)

<a id="case-evidence"></a>
## 保存された判断を確認する

[GenerationとSelection](../architecture.md#store-authority)は別IDで、読取から政策を再適用しない。調査を始めると、Caseには対象・明示した比較相手の行、同世代モデル、cutoffが固定される。調べなかった全社の詳細を再開時に最新値で補わない。

私有application DBのコピーでOperationをID指定して開き、保存選定・進捗・正式提案をたどる。対象0件、未完、査読後の見送りは異なる結果で、後日の取引・振返りは当時の判断の根拠ではない。

```bash
uv run baibai-engine operation --db <PRIVATE_APPLICATION_COPY> --format json show
uv run baibai-engine operation --db <PRIVATE_APPLICATION_COPY> --format json show <OPERATION_ID>
uv run baibai-engine operation --db <PRIVATE_APPLICATION_COPY> --format json selection-evidence <OPERATION_ID> --selection-sha256 <PUBLISHED_SELECTION_SHA256> --proposal-id <PROPOSAL_ID>
```

最終行は[正式提案](../../engine/src/baibai_engine/investment_judgment/trade_proposal/query.py)が参照する選定原本を読む。公開時と運用用のSHAが違う場合は元の受領をたどる。提案がなければ取得を飛ばし、`show` の保存内容を読む。私有本文や出力を公開しない。
