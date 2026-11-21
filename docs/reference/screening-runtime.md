# 探索索引から調査対象を選ぶ

現行のScreeningSelectionは「何が価格に対して安い可能性を作るか」を調べるための索引。[日次統合](../../jobs/src/baibai_jobs/integration/models.py)が一つのModelGenerationから[普通株の観測](../../engine/src/baibai_engine/machine_analysis/screening/policies/discovery.py)を取り出して発行する。行があるのはその世代で対象だったという意味で、割安や買付可能を示さない。

## 発行物を固定して読む

[企業判断の開始手順](../../.agents/skills/investment-judgment/SKILL.md#開始と固定対象)で依頼の範囲・公表ID・情報締切を固定する。新規発見では、同じ問いに該当する会社を銘柄コード順に読める。複数の問いに該当する会社もある。ページ順や該当数は買い順位ではなく、比較のために開いた会社が調査・提案対象に自動で加わることもない。

過去の公表物にこの索引がない場合、新規発見の一覧は `unindexed_legacy_publication` を返す。「当時の対象が0社だった」という意味ではない。必要なら公表IDと比較銘柄を明示し、[当時の保存行](../../engine/src/baibai_engine/investment_judgment/workflow/selection_query.py)を照合する。保有点検では別に台帳が必要で、台帳未確認は保有ゼロではない。現在の台帳を事実時刻で再生するため、過去の約定を後から記録すると同じ締切の対象が変わり得る。

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

- `input_gap` は、入力や価格の基準が未確定の会社を調べる問い。たとえば金融業で営業利益率が非該当なら、減収で増益成長の条件も満たさない会社を「営業利益率の取得漏れ」とは扱わない。一方、増収でも営業利益が未取得なら成長の判断は保留する。
- `outside_display` は、発行時の索引で `screening_lane` が `no_current_hypothesis` の会社を調べる問い。過去の画面に載ったかどうかは見ない。

同じ会社が両方に該当することがある。いずれも棄却や割安の判定ではない。[原典・時点・不足の規則](model-observations.md#source-owners)

<a id="case-evidence"></a>
## 保存された判断を確認する

索引を使って調査を始めると、[調査案件（Operation）に保存する選定原本](../../engine/src/baibai_engine/investment_judgment/workflow/case_screening.py)には、対象・明示した比較相手について発行時にあった行が残る。元の[GenerationとScreeningSelection](../architecture.md#store-authority)は別IDで、読取時に選び直さない。案件の情報締切や同世代のモデルを、再開時の最新値に差し替えない。

私有application DBのコピーでOperationをID指定して開き、保存選定・進捗・正式提案をたどる。対象0件、未完、査読後の見送りは異なる結果で、後日の取引・振返りは当時の判断の根拠ではない。

```bash
uv run baibai-engine operation --db <PRIVATE_APPLICATION_COPY> --format json show
uv run baibai-engine operation --db <PRIVATE_APPLICATION_COPY> --format json show <OPERATION_ID>
uv run baibai-engine operation --db <PRIVATE_APPLICATION_COPY> --format json selection-evidence <OPERATION_ID> --selection-sha256 <PUBLISHED_SELECTION_SHA256> --proposal-id <PROPOSAL_ID>
```

最後の例は[正式提案のIDと選定原本のSHAを照合する読取](../../engine/src/baibai_engine/investment_judgment/workflow/selection_evidence.py)。公開時と現在の運用用のSHAが違う場合、移行時に保存した原文と両方のSHAを照合できたときだけ、当時の本文が返る。提案がなければこの照合は不要で、`show` の保存内容を読む。私有本文や出力を公開しない。
