# Screening

保存モデルの技術・市場環境・財務を探索に使い、財務・成長を軸に理由付き少数Researchへ進む。候補や表示順は価値評価・買付採否ではなく、機能受入と投資効果の検証を分ける。採用方針・残る本番受入は[#2184](https://github.com/koumatsumoto/baibai-loop/issues/2184)に残す。

## 複数の発見根拠から財務仮説・L3へ

通常生成の同じstock plan全社で[TA owner](../../engine/src/baibai_engine/machine_analysis/screening/policies/technical_discovery.py)、[財務・成長 owner](../../engine/src/baibai_engine/machine_analysis/screening/policies/financial_growth.py)、既存4財務経路の座標を読む。T非合致・価格不足でも財務の発見理由を落とさず、Gを全企業の普遍的な合否にしない。旧財務上位候補や経済順位を入口・再足切りに使わない。SMA75/200・当日価格と活動、市場下落日の底堅さは探索理由で、クロス成立、機関買い、買い時を認定しない。市場条件別代金と価格経路の形状は同世代の詳細に残り、別々の平均を同日の価格×活動の交差へ変換しない。

新しい保存政策は全社のT/G状態、条件、既知非充足・不足とmodel revisionを残す。原数値・測定窓・財務比較の元観測は同じGenerationの完全モデルを明示詳細で読む。Gの`research_candidate`は利益成長/営業回復の調査仮説、`input_unresolved`は業種・比較・欠測を確かめる予備調査で、自動合格ではない。`not_selected`はこの成長/回復条件で非合致であり、会社不適格やProposal actionではない。財務経路のeligibleも割安・健全性や採用推奨でなく、原観測と制約を持つ別の仮説である。資金耐久・資金化・希薄化・将来成長はL3で調べる。

表示は企業重複を除く決定的な経路巡回。v3以降の予備レーンはそのレーン内だけで巡回し、各経路内を会社共通の固定hash順にする。他レーンを通過する既存経路と、予備だけの小さい確認queueとの露出差を抑えるためで、成長/代替レーンのコード順は変えない。複数経路に同じ会社が属しても別々のhashを与えない。小群・経路重複の露出差と、同じ先頭ページを繰り返す偏りは残る。36はページの最大件数で、候補足切り、投資順位、最適配分、深掘り予算ではない。枠消化の補充や合致数の加点はせず、経路の独立性・機会の均等な抽出・利益優位を主張しない。

三つのレーンを分ける。通常表示は `growth_research`（Gの成長/回復仮説）。`input_review` はG未確定かつ技術/市場または財務経路に調べる根拠がある予備調査、`alternative_financial_research` はG非合致でも別の財務経路がある代替仮説である。後二者は同じ画面に件数と `--screening-lane` の切替を示し、隠れた除外にしない。代替でもGの既知悪化・不足を残し、技術の強さや別経路の存在で成長合格へ変更しない。どのレーンも正式Research採択や買付判断ではない。

株数基準だけが未確定でも、選択済み収益力/増収の事業根拠がある場合は価格入力を調べる余地がある。v2以降では、G非合致かつ財務経路非合致のうち、正の同日株価・必要な事業operand/比較basisと明示的な株数不足を確認できるものに限り、`input_review`へ別の確認理由を残す。Gの既知悪化、原経路の非適格、価格未評価は変えず、株数の補完や財務合格ではない。業種不足一般や旧FYへの選び直しには広げない。v3以降のbriefは予備レーンのG未確定/非合致の母数を併記するが、これを調査枠や採択数にしない。

v4では、同じ採用済み事業仮説があり、株数・時価総額が正でも価格日だけが古い明示的な`stale_price`を価格入力確認へ残す。古い観測日と必要日を併記し、Tの非合致/利用不能、Gの既知非充足、Fの非適格・価格座標nullを保持する。未観測価格一般や他の不足を救済せず、値を現在価格へ繰り上げない。停滞の長さ・取引停止・流動性・現在の取引可否は別途確認が必要で、このレーンへの掲載を買付可能性や価値の支持にしない。新規生成だけをv4とし、過去v1/v2/v3の保存内容・表示順・固定Research入力は再評価せず読み続ける。

この分離は、T必須による財務候補の漏れと、全経路ORによる無差別な拡大を避けて比較できるようにする設計仮説である。既存の本番由来断面で両方の残差を確認したが、古いrecipeの観測差・後日の再生を当時の公開政策や利益優位へ読み替えない。未知/非合致の全状態は保存し、レーン外も明示詳細で調べられる。閾値・レーン・表示順の最適性は未検証で、通常データとL3判断差から改善する。

初回の`selection-context`が返すSelection IDとcutoffを固定して、同じ `--screening-lane` の `--screening-page <next>`で容量外も読む。`candidate_tickers`は表示中のページ、`summary`は全stock planの分母。`--compare-ticker`は表示外・G非合致・T非合致も、保存した状態/理由と同世代の完全モデルを読む。ページ読取で政策の再適用、入力の再生成、Research開始は行わない。

L3は同世代詳細・既存調査・市場/業界/世界動向・外部評価のずれから、既存容量の最大3対象を理由付きで選ぶ。未確定を選ぶなら先に答える問い、既知非充足の例外を選ぶなら根拠を明示する。TAの値動きと新しい事業根拠を分け、価値・買値・待機・最強反証・資金/共通リスクを既存Assessment/Review/Proposalへつなぐ。新しい人物score・一律MA撤退・自動売買は設けない。

<a id="rule-purpose"></a>
## 保存済み経済経路政策の意味

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

旧五入口の比較を再生する[読取専用の実験caller](../../tools/experiments/inspect_technical_financial_candidates.py)で同じ保存Generationの広いstock planを調べられる。TAの入口に合致した企業だけ財務・成長案へ渡し、非合致・未判定・財務未評価も分母に残す。旧財務候補への所属は前提にせず、複数入口への合致で企業を重複評価しない。順位・固定配分・候補数を埋める補充は行わない。

```bash
uv run python -m tools.experiments.inspect_technical_financial_candidates \
  --generation-id <FIXED_GENERATION_ID> --runs-path <READ_ONLY_RUNS_COPY> \
  --out <NEW_PRIVATE_JSON>
```

これは旧研究条件の比較であり、日次ScreeningSelectionを再適用・保存する機能ではない。研究wrapperの既存labelはこの比較callerを識別するもので、同じ財務条件を採用した新しい通常政策の状態を表すものではない。TAの現状態を新規イベント検出と呼ばず、対市場プラスを個社上昇と読み替えない。財務の`research_candidate`は原典調査を続ける仮説であり、財務健全性・有機成長の合格や買付採否ではない。成長モデルだけで仮説が成立しても、財務モデル未生成やCF・負債の欠測は残る。観測された悪化と未確定operandを併記し、未評価・見送り・未確定をProposalのactionへ変換しない。

経路ごとの不足は、L2が記録した業種不適用・比較可能性未確定・その他の利用不能へ分ける。これは保存L2の適用規則の説明であり、その会社に利益率の経済的意味がないとの新判断ではない。その他には未観測だけでなく非正の分母等もあり、全て追加取得で埋まる欠測とはせず、元のreason・既知非充足も読む。銀行等の不適用を赤字へ変換せず、同じOP/利益率の再取得を待つだけにもせず、業種に合う利益・資本・資金構造を有限調査の問いへ残す。親会社利益や経常利益をOPへ代入せず、業種名だけの自動合格/除外は行わない。不使用の回復経路で利益率がN/Aでも、通常利益成長経路の成立は消さない。

比較元の保存quarter観測も対応する比較科目付きで補助表示し、上位の`operand_unavailable`と元の版不整合・basis・分母制約等を分けて読む。同じ保存比較に対応する観測だけを辿り、複数の元観測を一つへ選別せず、原reason・欠落field・値を保持する。対応科目なし/空配列はそのraw観測を提示できない意味で、経済的な悪化や取得失敗の認定ではない。曖昧な比較やPDF由来の元観測はこの補助表示の対象外で、元比較のreason/sourceを読む。追加取得・逆算・欠測補完、上位reason/合否の置換はしない。

回復経路が成立した場合は当四半期と保存された前年同四半期のOP符号を示し、赤字縮小・損益均衡・黒字化・黒字の改善を区別して読めるようにする。前年符号未記録を逆算せず、年次赤字・減収・CF/負債欠測を救済したとも扱わない。これらは調査内容を分ける診断であり、既存の合否条件・3状態・閾値は変更せず、資金耐久力や将来成長の認定を追加しない。

後続は既存の`selection-context --compare-ticker`で同じ世代とcutoffの候補外詳細を読む。`discover_new`では対応する保存Selectionを`--screening-selection-id`で明示し、generation_id一致を確認する。残す対象・問い・最強反証を理由付きResearch Selectionへ明示し、保存財務Selectionは比較元のまま保持してTA診断の保存結果と偽装しない。深掘り容量は既存Research policyの範囲で、診断の合致件数をそのままOperationへ渡さない。市場・世界/業界・外部評価との差、資金耐久力・普通株価値・価格はL3で調べる。callerはResearch開始・保存・売買を行わない。

保存as_of、期待価格日、計算時刻は別の時計である。出力は消費者への配布/受領証跡、完全PIT、前向き登録、成熟した将来成績を証明しない。旧世代の未記録fieldを現在の値で埋めず、新政策の本番受入や利益改善とは別に扱う。

### 保存TAの状態と遷移を分ける

[二世代比較の研究診断](../../tools/experiments/inspect_technical_transitions.py)は、現行TA入口の式を変えず、明示した前後のstock planのunionと両方の元診断を残す。

```bash
uv run python -m tools.experiments.inspect_technical_transitions \
  --previous-generation-id <PREVIOUS_ID> --current-generation-id <CURRENT_ID> \
  --runs-path <READ_ONLY_RUNS_COPY> --out <NEW_PRIVATE_JSON>
```

異なる計算時刻だけで新規イベントにしない。同じ価格日、非連続価格日、policy/model revision不一致、価格/モデル欠測は比較不能として理由を残す。両断面で既知の経路だけ非合致→合致・継続・解消を数え、非充足と欠測が併存する断面からの合致も入力回復へ分ける。経路の`known_state_pairs=0`は計測不能であり、新規0件を確認した意味ではない。新規母集団入りも価格変化としない。

市場相対は前後の比較指数が既知かつ同一であることも必要とする。指数変更・未記録は当該経路だけ比較不能とし、理由を`route_changes`に残す。企業単位の価格比較可否や現状態は消さず、指数に依存しない他のTA経路も巻き込まない。

企業単位で前日から候補でも、別経路の新しい合致を落とさない。一方で遷移だけをhard gateにせず、持続する状態も読めるように保つ。保存時刻は配布/受領・取引可能時刻や当時PITを証明しない。これは観測比較であって、財務gate・Research選定・日次政策・利益検証へは自動接続しない。非zero終了の出力は採用せず、私有JSONを公開log/artifactへ出さない。

## 入力不足と品質

不足は[観測の規則](model-observations.md#source-owners)に従い局所化し、[期間/株式basis](model-observations.md#financial-periods)を揃える。要求範囲の取得coverageを全銘柄充足、不足による候補減を機会減としない。

候補カードの保存価格basis・不足と通常生成時の価格入力集計を使い、個別の詳細は前述の`--compare-ticker`へ進む。新規Selectionには日次通知と同じ集計を保存し、初回比較と固定Researchへ渡す。旧成果の集計欠落は未記録であり、0や現在の値で補完しない。

新政策の`price_inputs.basis`はTA価格経路の準備状態を示し、財務モデルの価格basisや現在の取引可否とは別である。集計は保存世代の価格入力の状態で、現在の鮮度や全財務入力の充足を保証しない。候補0件でも価格入力不足・母集団0件を区別し、価格入力が有効というだけで「投資機会なし」とは判断しない。

## 保存と読取

ModelGenerationとScreeningSelectionは別identity。候補政策は通常モデル生成時に適用する。保存Generationへの現政策の再適用と、保存母集団全社の価格/株数不足一覧診断は提供しない。日次再実行は固定Generationへの再適用の代替ではない。候補0件も正常に公開し、過去の非空候補へ戻さない。保存/時点選択は[store](../../engine/src/baibai_engine/machine_analysis/screening/selection_store.py)と[query](../../engine/src/baibai_engine/machine_analysis/screening/selection_query.py)、L3再開/cache寿命は[保存責務](../architecture.md#store-authority)、取得/retry/発行/整理は[日次運用](../../jobs/OPERATIONS.md#日次実行と結果確認)へ。

新政策の初回本番受入は、既存runの終端・writer/lease・実行commitを確認し、[日次の明示保持](../../jobs/OPERATIONS.md#日次実行と結果確認)で既存全Generation/Selectionを残す。保持はその実行だけなので、受入後の通常cache整理と長期保管を同一視しない。

新しい政策結果を保存した後は、それを解釈できない旧codeへ全体を戻さない。切戻す場合も新結果と固定Researchのreader・現行lease ownerを維持し、次回以降の政策変更を別の検証済み差分にする。旧bundleの盲復元、新Selection削除、同じidentityへの旧政策上書きで切戻さない。

<a id="decision-cohort-diagnostic"></a>
## 現行判断の比較前診断

[読取専用tool](../../tools/experiments/inspect_decision_cohort.py)は完了した一回のOperationの保存入力・評価pair・Proposalを照合する。行は候補と調査対象のunionで、0対象でも候補を保つ。行の入力は運用用Selectionであり、公開時ProposalのSelectionとは役割とhashを分けて示す。正当なID移行は既存receiptで照合し、原公開canonicalを現行モデルや経済政策の証拠へ読み替えない。未選定・未完・経済的見送りを区別し、全開始Operationを分母にした比較、第二の判断正本や旧較正の再生成には使わない。

```bash
uv run python -m tools.experiments.inspect_decision_cohort \
  --db <PRIVATE_APPLICATION_COPY> --operation-id <COMPLETED_OPERATION_ID> \
  --out <NEW_PRIVATE_JSON>
```

成果・指値の因果効果・当時PIT・事前登録は証明せず、hash一致も事前固定を意味しない。正式比較の対象/窓/metricは結果を見る前に決める。完了時計（`baseline_available_at`）を約定可能時刻とせず、選定/提案は各公開以後の観測と別々に比べ、取引可能時刻も確認する。後日の訂正・本人報告・取引・Retrospectiveを当時本文に混ぜず、私有出力を公開log/artifactへ出さない。
