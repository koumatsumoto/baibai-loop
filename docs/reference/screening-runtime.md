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

## TA探索と財務・成長案の有限診断

通常政策を変更する前に、[読取専用の実験caller](../../tools/experiments/inspect_technical_financial_candidates.py)で同じ保存Generationの広いstock planを調べられる。TAの入口に合致した企業だけ財務・成長案へ渡し、非合致・未判定・財務未評価も分母に残す。旧財務候補への所属は前提にせず、複数入口への合致で企業を重複評価しない。順位・固定配分・候補数を埋める補充は行わない。

```bash
uv run python -m tools.experiments.inspect_technical_financial_candidates \
  --generation-id <FIXED_GENERATION_ID> --runs-path <READ_ONLY_RUNS_COPY> \
  --out <NEW_PRIVATE_JSON>
```

これは未採択の候補政策の比較であり、日次ScreeningSelectionを再適用・保存する機能ではない。TAの現状態を新規イベント検出と呼ばず、対市場プラスを個社上昇と読み替えない。財務の`research_candidate`は原典調査を続ける仮説であり、財務健全性・有機成長の合格や買付採否ではない。成長モデルだけで仮説が成立しても、財務モデル未生成やCF・負債の欠測は残る。観測された悪化と未確定operandを併記し、未評価・見送り・未確定をProposalのactionへ変換しない。

経路ごとの不足は、L2が記録した業種不適用・比較可能性未確定・その他の利用不能へ分ける。これは保存L2の適用規則の説明であり、その会社に利益率の経済的意味がないとの新判断ではない。その他には未観測だけでなく非正の分母等もあり、全て追加取得で埋まる欠測とはせず、元のreason・既知非充足も読む。銀行等の不適用を赤字へ変換せず、同じOP/利益率の再取得を待つだけにもせず、業種に合う利益・資本・資金構造を有限調査の問いへ残す。親会社利益や経常利益をOPへ代入せず、業種名だけの自動合格/除外は行わない。不使用の回復経路で利益率がN/Aでも、通常利益成長経路の成立は消さない。

回復経路が成立した場合は当四半期と保存された前年同四半期のOP符号を示し、赤字縮小・損益均衡・黒字化・黒字の改善を区別して読めるようにする。前年符号未記録を逆算せず、年次赤字・減収・CF/負債欠測を救済したとも扱わない。これらは調査内容を分ける診断であり、既存の合否条件・3状態・閾値は変更せず、資金耐久力や将来成長の認定を追加しない。

後続は既存の`selection-context --compare-ticker`で同じ世代とcutoffの候補外詳細を読む。`discover_new`では対応する保存Selectionを`--screening-selection-id`で明示し、generation_id一致を確認する。残す対象・問い・最強反証を理由付きResearch Selectionへ明示し、保存財務Selectionは比較元のまま保持してTA診断の保存結果と偽装しない。深掘り容量は既存Research policyの範囲で、診断の合致件数をそのままOperationへ渡さない。市場・世界/業界・外部評価との差、資金耐久力・普通株価値・価格はL3で調べる。callerはResearch開始・保存・売買を行わない。

保存as_of、期待価格日、計算時刻は別の時計である。出力は消費者への配布/受領証跡、完全PIT、前向き登録、成熟した将来成績を証明しない。旧世代の未記録fieldを現在の値で埋めず、本番採用や利益改善は別途検証する。

### 保存TAの状態と遷移を分ける

[二世代比較の研究診断](../../tools/experiments/inspect_technical_transitions.py)は、現行TA入口の式を変えず、明示した前後のstock planのunionと両方の元診断を残す。

```bash
uv run python -m tools.experiments.inspect_technical_transitions \
  --previous-generation-id <PREVIOUS_ID> --current-generation-id <CURRENT_ID> \
  --runs-path <READ_ONLY_RUNS_COPY> --out <NEW_PRIVATE_JSON>
```

異なる計算時刻だけで新規イベントにしない。同じ価格日、非連続価格日、policy/model revision不一致、価格/モデル欠測は比較不能として理由を残す。両断面で既知の経路だけ非合致→合致・継続・解消を数え、非充足と欠測が併存する断面からの合致も入力回復へ分ける。経路の`known_state_pairs=0`は計測不能であり、新規0件を確認した意味ではない。新規母集団入りも価格変化としない。

企業単位で前日から候補でも、別経路の新しい合致を落とさない。一方で遷移だけをhard gateにせず、持続する状態も読めるように保つ。保存時刻は配布/受領・取引可能時刻や当時PITを証明しない。これは観測比較であって、財務gate・Research選定・日次政策・利益検証へは自動接続しない。非zero終了の出力は採用せず、私有JSONを公開log/artifactへ出さない。

## 入力不足と品質

不足は[観測の規則](model-observations.md#source-owners)に従い局所化し、[期間/株式basis](model-observations.md#financial-periods)を揃える。要求範囲の取得coverageを全銘柄充足、不足による候補減を機会減としない。

候補カードの保存価格basis・不足と通常生成時の価格入力集計を使い、個別の詳細は前述の`--compare-ticker`へ進む。新規Selectionには日次通知と同じ集計を保存し、初回比較と固定Researchへ渡す。旧成果の集計欠落は未記録であり、0や現在の値で補完しない。

集計は保存世代の価格入力の状態で、現在の鮮度や全財務入力の充足を保証しない。候補0件でも価格入力不足・母集団0件を区別し、価格入力が有効というだけで「投資機会なし」とは判断しない。

## 保存と読取

ModelGenerationとScreeningSelectionは別identity。候補政策は通常モデル生成時に適用する。保存Generationへの現政策の再適用と、保存母集団全社の価格/株数不足一覧診断は提供しない。日次再実行は固定Generationへの再適用の代替ではない。候補0件も正常に公開し、過去の非空候補へ戻さない。保存/時点選択は[store](../../engine/src/baibai_engine/machine_analysis/screening/selection_store.py)と[query](../../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)、L3再開/cache寿命は[保存責務](../architecture.md#store-authority)、取得/retry/発行/整理は[日次運用](../../jobs/OPERATIONS.md#日次実行と結果確認)へ。

<a id="decision-cohort-diagnostic"></a>
## 現行判断の比較前診断

[読取専用tool](../../tools/experiments/inspect_decision_cohort.py)は完了した一回のOperationの保存入力・評価pair・Proposalを照合する。行は候補と調査対象のunionで、0対象でも候補を保つ。行の入力は運用用Selectionであり、公開時ProposalのSelectionとは役割とhashを分けて示す。正当なID移行は既存receiptで照合し、原公開canonicalを現行モデルや経済政策の証拠へ読み替えない。未選定・未完・経済的見送りを区別し、全開始Operationを分母にした比較、第二の判断正本や旧較正の再生成には使わない。

```bash
uv run python -m tools.experiments.inspect_decision_cohort \
  --db <PRIVATE_APPLICATION_COPY> --operation-id <COMPLETED_OPERATION_ID> \
  --out <NEW_PRIVATE_JSON>
```

成果・指値の因果効果・当時PIT・事前登録は証明せず、hash一致も事前固定を意味しない。正式比較の対象/窓/metricは結果を見る前に決める。完了時計（`baseline_available_at`）を約定可能時刻とせず、選定/提案は各公開以後の観測と別々に比べ、取引可能時刻も確認する。後日の訂正・本人報告・取引・Retrospectiveを当時本文に混ぜず、私有出力を公開log/artifactへ出さない。
