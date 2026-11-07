# Screening

保存モデルから調査候補を抽出する。モデル生成が先行し、候補外もInvestment Judgmentから読める。候補順位は注意配分で、企業の経済的reject・価値評価・買付採否ではない。

<a id="rule-purpose"></a>
## 候補抽出の意味

収益力・事業拡大・営業回復・資本活用ごとに価格と事業を組み合わせる。計算・容量・同順位は[順位付け](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_paths.py)、座標・期間・参照群は[入力投影](../../engine/src/baibai_engine/machine_analysis/screening/policies/economic_path_inputs.py)が所有する。

相対上位は市場全体が割高でも生じる。業種内の異質性、少数参照群の百分位上限、有効経路数の機会差、同じ利益変化や一過性利益による偏りを残す。順位は期待収益・確率・健全性でもなく、二座標/複数モデルを独立票にしない。候補の入替え≠品質/将来収益改善であり、有限の政策比較と採否は[#1680](https://github.com/koumatsumoto/baibai-loop/issues/1680)に残す。

経路ごとの条件は全企業の除外条件ではない。

- 成長/回復は現在の利益/CFの黒字を要さない。売上/営業改善を株式時価総額で割っても負債込み事業価値の割安性は保証せず、追加投資・借換・希薄化はL3で評価する。
- 完全FYはTTMの対応期未観測を補うだけで、既知の悪化・欠測・basis不一致を消さない。選択FYと後続報告/最新終端TTMを分ける。収益力≠成長の主張で、横ばい/小幅減益だけで除外しない。
- 資本経路は正の報告利益を要するが、利益/簿価は保存PBRの含意簿価に対するproxy。簿価の個別source・会計scope・測定日は未保存で、検証済み普通株ROE/平均自己資本利益率/資産解放額ではなく、未実現資産の全機会も拾わない。

<a id="candidate-discovery"></a>
## 調査への引渡し

カードは原数値・符号・対象期・反対材料を保つ。L3は一次資料・複数モデル・必要な少数の候補外対照で価値獲得経路と反証、一過性利益を評価する。外部評価・テクニカル・共有環境は比較材料で、保存市場判断/portfolioは機械順位を変えない。[保存旧成果](estimate-calibration.md)も現在の採否へ読み替えない。

同じ比較の詳細を `selection-context --compare-ticker` で再読する時は、返されたSelection ID・`research_as_of`・`input_known_at`を保ち、`--screening-selection-id`・`--research-asof`・timezone付きの`--known-at`へ渡す。Selection IDはモデル世代を固定し、Research・市場判断は各保存/公開時刻をcutoffと照合して読む。`--known-at`省略時は実行時刻を使う。

cutoffだけでcontext全体は固定されない。保有は現在の台帳を発生時刻でreplayするため、過去の注文/約定の後追記で同じcutoffでも保有と依頼対象が変わり得る。contextの再読を保存Operationの固定入力からの再開と区別する。古いcutoffの指定は、後で取得した原本を当時既知だった証拠に変えない。

## 入力不足と品質

[原典の時点・欠測](model-observations.md#source-owners)と[期間/株式basis](model-observations.md#financial-periods)を守る。不足は該当材料/経路に局所化し、候補数のために0や別期の好値で埋めない。取得coverageは成功した要求範囲で全銘柄の入力充足ではなく、入力不足による候補減少を機会減少としない。

候補外の価格・株数不足を調べる時は `investment-judgment selection-context --include-pricing-gaps` を使う。保存Selectionの母集団と同じGenerationのCompany Financialsだけを追加で読み、現行の価格条件を満たさない銘柄と保存値・欠測理由をコード順で返す。過去の除外理由の再現や候補順位ではなく、価格日不一致と株数不足の重複も残す。確認対象は同じ `--screening-selection-id` と `--compare-ticker` で詳細を読める。株数の補完・再選定・保存物の変更は行わない。

## 保存と読取

ModelGenerationとScreeningSelectionは別identity。同じモデルの政策変更は再生成ではない。候補0件も正常に公開し、過去の非空候補へ戻さない。保存/時点選択は[store](../../engine/src/baibai_engine/machine_analysis/screening/selection_store.py)と[query](../../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)、L3再開/cache寿命は[保存責務](../architecture.md#store-authority)、取得/retry/発行/整理は[日次運用](../../jobs/OPERATIONS.md#日次実行と結果確認)へ。

<a id="decision-cohort-diagnostic"></a>
## 現行判断の比較前診断

[読取専用tool](../../tools/experiments/inspect_decision_cohort.py)は、完了した一回のOperationの母集団・候補・Selection・評価pair・Proposalを照合する。現在値を補わず、第二の判断正本や旧較正の再生成にはしない。

```bash
uv run python -m tools.experiments.inspect_decision_cohort \
  --db <PRIVATE_APPLICATION_COPY> --operation-id <COMPLETED_OPERATION_ID> \
  --out <NEW_PRIVATE_JSON>
```

行は当時の候補と調査対象のunion、モデルrevisionは保存Selectionのslice。0対象でも候補を保ち、未選定≠経済的見送り、未完≠`no_action`、提案なしを捏造しない。他の未完/中断Operationや全母集団の詳細は含まず、全開始件数を分母にした方法比較ではない。私有出力を公開ログ/artifactへ転送しない。

成果・指値の因果効果・当時PIT・事前登録は証明しない。hash一致も事前固定でなく、正式比較の対象/窓/metricは結果を見る前に固定する。`baseline_available_at`は完了時計で約定可能時刻ではない。選定は`screening_published_at`、最終提案は`proposal_published_at`以後の観測で別に比べ、価格観測/取引可能時刻も確認する。後日の訂正・本人報告・取引・Retrospectiveを当時の本文へ混ぜない。
