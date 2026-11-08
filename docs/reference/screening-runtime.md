# Screening

保存モデルから調査候補を抽出する。候補外もInvestment Judgmentから読め、順位は注意配分であって経済的reject・価値評価・買付採否ではない。

<a id="rule-purpose"></a>
## 候補抽出の意味

収益力・事業拡大・営業回復・資本活用ごとに価格と事業を組み合わせる。計算・容量・同順位は[順位付け](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_paths.py)、座標・期間・参照群は[入力投影](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_path_inputs.py)が所有する。

市場全体が割高でも相対上位は生じる。業種内の異質性・少数参照群・有効経路数・一過性利益の偏りがあり、順位を期待収益・確率・健全性、複数モデルを独立票にしない。候補入替えは品質/将来収益改善の証拠でなく、有限の政策比較と採否は[#1680](https://github.com/koumatsumoto/baibai-loop/issues/1680)に残す。

経路ごとの条件は全企業の除外条件ではない。

- 成長/回復は現在の利益/CFの黒字を要さない。売上/営業改善を株式時価総額で割っても負債込み事業価値の割安性は保証せず、追加投資・借換・希薄化はL3で評価する。
- 完全FYはTTMの対応期未観測を補うだけで、既知の悪化・欠測・basis不一致を消さない。選択FYと後続報告/最新終端TTMを分ける。収益力≠成長の主張で、横ばい/小幅減益だけで除外しない。
- 資本経路は正の報告利益を要するが、利益/簿価は保存PBRの含意簿価に対するproxy。簿価の個別source・会計scope・測定日は未保存で、検証済み普通株ROE/平均自己資本利益率/資産解放額ではなく、未実現資産の全機会も拾わない。

<a id="candidate-discovery"></a>
## 調査への引渡し

カードは原数値・符号・対象期・反対材料を保つ。L3は一次資料・複数モデル・必要な候補外対照で価値獲得経路と反証を評価する。外部評価・テクニカル・共有環境は比較材料で、保存市場判断/portfolioは機械順位を変えない。[保存旧成果](estimate-calibration.md)も現在の採否へ読み替えない。

詳細の再読は `selection-context --compare-ticker` を使う。返されたSelection ID・`research_as_of`・`input_known_at`を、それぞれ`--screening-selection-id`・`--research-asof`・timezone付き`--known-at`へ渡す。モデル世代とcutoffを保ち、Research・市場判断は各保存/公開時刻をcutoffと照合する。`--known-at`省略時は実行時刻。

保有は現在台帳を発生時刻でreplayするため、過去の注文/約定の後追記で同じcutoffでも保有と依頼対象が変わり得る。context再読を保存Operationの固定入力からの再開と分け、後日取得を当時既知の証拠にしない。

## 入力不足と品質

不足は[観測の規則](model-observations.md#source-owners)に従い局所化し、[期間/株式basis](model-observations.md#financial-periods)を揃える。要求範囲の取得coverageを全銘柄充足、不足による候補減を機会減としない。

候補カードの保存価格basis・不足と通常生成時の価格入力集計を使い、個別の詳細は前述の`--compare-ticker`へ進む。

## 保存と読取

ModelGenerationとScreeningSelectionは別identity。候補政策は通常モデル生成時に適用する。保存Generationへの現政策の再適用と、保存母集団全社の価格/株数不足一覧診断は提供しない。日次再実行は固定Generationへの再適用の代替ではない。候補0件も正常に公開し、過去の非空候補へ戻さない。保存/時点選択は[store](../../engine/src/baibai_engine/machine_analysis/screening/selection_store.py)と[query](../../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)、L3再開/cache寿命は[保存責務](../architecture.md#store-authority)、取得/retry/発行/整理は[日次運用](../../jobs/OPERATIONS.md#日次実行と結果確認)へ。

<a id="decision-cohort-diagnostic"></a>
## 現行判断の比較前診断

[読取専用tool](../../tools/experiments/inspect_decision_cohort.py)は完了した一回のOperationの保存入力・評価pair・Proposalを照合する。行は候補と調査対象のunionで、0対象でも候補を保つ。未選定・未完・経済的見送りを区別し、全開始Operationを分母にした比較、第二の判断正本や旧較正の再生成には使わない。

```bash
uv run python -m tools.experiments.inspect_decision_cohort \
  --db <PRIVATE_APPLICATION_COPY> --operation-id <COMPLETED_OPERATION_ID> \
  --out <NEW_PRIVATE_JSON>
```

成果・指値の因果効果・当時PIT・事前登録は証明せず、hash一致も事前固定を意味しない。正式比較の対象/窓/metricは結果を見る前に決める。完了時計（`baseline_available_at`）を約定可能時刻とせず、選定/提案は各公開以後の観測と別々に比べ、取引可能時刻も確認する。後日の訂正・本人報告・取引・Retrospectiveを当時本文に混ぜず、私有出力を公開log/artifactへ出さない。
