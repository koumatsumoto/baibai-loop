---
name: research
description: 人間が選んだ候補を一次情報で深掘りし、thesis、独立反証、candidate評価と資本配分または見送りの統合判断まで確定する。候補の提示までは research-triage skill。
---

# Research

## 目的

Research Triage から人間が選んだ候補を一次情報で検証し、buy または理由付きの見送りへ確定する。broker 操作と発注は人間が行う。

`normalized_per_3fy`はNormalized Earnings Powerのnative eligibility/order座標であり、ResearchのFV/E[r] estimator入力へ転用しない。

## 開始条件

- 人間がcanonical Research Triageの`research`候補からResearch Setを選ぶ。依頼がsubsetを指定した場合は、その範囲だけを扱う。
- `research prepare --ticker`が選択集合とexact Triageを同時に固定し、ここで初めて`capital-allocation` Operationを開始する。別Operationがactiveなら新しいResearchを開始しない。
- 空Research Setは正常で、workspaceは`no_research`となりOperationを作らない。

prepare後は、そのactiveな`capital-allocation` Operationをresumeする。Operationはcanonical Research Triageをartifact / canonical refに持つ。別sessionを開始しない。同じ`as_of`だけの別Operationを採用せず、workspaceに固定したexact TriageとResearch Setのbindingを使う。

## 1. Workspace を準備する

```bash
uv run baibai-engine research prepare \
  --research-triage-id <RESEARCH_TRIAGE_ID> \
  --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF> \
  --ticker <SELECTED_TICKER>  # 選んだtickerごとに反復
```

as-of、最大80件の比較snapshot、Researchへ進められるtickerはapplication DBのpublished Research Triageから導出する。Review Set fileやrun storeはResearch開始後のauthorityではない。E[r]較正contextは`research-workspace.yaml`の`er_realized_distribution_context`に置く。執筆済みworkspaceを切り替える場合は元directoryを保持し、別directoryへprepareして必要なThesis / Review draftと比較メモを引き継ぐ。比較メモはAssessment draftへ置き、as-of / hashの再検証とreviewを省略しない。

選択集合のauthorityはprepare時に固定したOperation bindingである。`thesis-scaffold`、`review-scaffold`、`promote`はactive Operationのexact TriageとResearch Set集合を再確認し、manifestとworkspaceの両方を書き換えてもadmitできない。`status`はOperation完了後のworkspaceも読み取れる。

## 2. Case ごとの thesis を確定する

各 case で次を行う。

1. `research thesis-scaffold` で thesis を作る。
2. 会社 IR、EDINET、決算資料などの一次資料で load-bearing claim を調べる。検索 snippet、二次情報、外部 AI 出力を観測事実にしない。playbook は `applies_to_valuation_approach_ids` の明示 mapping だけを使い、同名 slug から implicitに対応を推測しない。[事業モデル別リサーチ](../../../docs/reference/business-model-research.md)は指定 playbook の補助に限る。
3. 未織込み、価値変化、実現経路、失敗・遅延、配当持続性、7軸を一次根拠で確認する。Playbookは問いとして使い、checklist完了印を公開gateにしない。非開示はunknownとし、重要性とdispositionへの影響を説明する。
4. Triageに束縛されたMacro Contextの`connection.estimate_caveats`を確認し、materialな含意だけをcalculation、investment case、反対仮説へ接続する。macroとE[r]はcontextで、単独gateにしない。技術・産業構造変化も同じ契約で扱う。
5. 一次根拠からBase/Downsideを一つのhorizonで組み立て、terminalと当該期間の累積分配を区別する。赤字回復を正の起点利益へ捏造せず、NI×PER/EV、負債、分割・自己株・希薄化の単位と二重算入を検算する。`evaluate <thesis>`の原価格の`valuation_context`を読み、必要terminalと独立に見積もった妥当範囲を比較する。必要額に合わせて予測を引き上げず、判断を左右する少数の仮定を既存calculation・investment case・countercaseへ書く。倍率回復なしを検算し、遅延がmaterialなら利益・分配・借換え・株数の連動を含む経済条件を組み直す。[計算の限界と記入例](../../../docs/reference/thesis.md#valuation-context)に従い、固定価値のh+12を実際の遅延検証としない。Review未添付だけの`review_required`は計算資料が出てもexit 2のままで、errorsを確認し他の失敗を握りつぶさない。
6. `review-scaffold`の独立検算欄は作者値をコピーせず、別の作業者がsourceと計算を再確認する。`evaluate <thesis> --review <review>`でsource・単位・hash・terminal/cash不一致を解消する。contextのコピーを独立検算とせず、経済的反証をReviewの`strongest_countercase`へ書く。core修正後はReviewを取り直し、企業別Reviewに候補比較を重複させない。
7. 価格や評価額が不明でも、根拠付きunresolvedとdefer/rejectを完成させる。重要な証拠不足を小口購入やoverrideで通さない。返済条件などmaterialな事実不明と、根拠を伴う将来見積りの幅を区別し、未来が未確定という理由だけでcandidateを排除しない。根拠不足はdefer等を維持し、本当に非重要なunknownだけReviewで理由を説明する。

## 3. 比較して disposition を決める

全 case を `candidate` / `defer` / `reject` まで進め、原評価日・horizon・Base/Downside・要求利回り・countercase・disposition を横断比較する。review 後に `research promote` で全 case を canonical にし、`research status`の`cases`で各tickerの調査・review・promoteの残作業を確認する。`next_action`は作業説明であり、実行時の引数はpublic `--help`で確認する。`published`は現在のThesis coreとReviewがcanonical publicationに一致することを示し、AssessmentやOperationの完了は意味しない。横断比較と最終dispositionはAssessment draftだけに書く。

## 4. Assessment とcontent reviewを公開する

`research capital-allocation-scaffold` で promote 済みの全 case を Capital Allocation Assessment に含め、既存`comparison / forgone / alternatives[].rationale`に、重要仮定とその根拠、不利な条件での見返りから採用／見送りを説明する。Base年率やPmax内だけで選ばず、倍率上昇と業績回復への依存を区別し、一律順位にしない。診断数値を機械fieldとして複写せず、人間には結論・少数の成立仮定・提案が変わる反対条件を伝える。全出力の読み合わせや確認段階を増やさない。research question が複数論点を含む場合は分割し、一部未解決のまま全体を `answered` にしない。

1. `research capital-allocation-publish --check` で digest を確認する。review 前の `review_binding=stale` は正常。
2. Capital Allocation Assessment author と別の役がcontent reviewを作る。
3. content digest が一致してから assessment と review を publish する。
4. deferred monitoring を task にする場合は、既存 task と重複しないことを確認して dated task を 1 件だけ作る。

## 5. Buy case の当日指値を確認する

`research plan-limit --capital-allocation-assessment-id <ASSESSMENT_ID>` は canonical Capital Allocation Assessment が `allocate` の alternative にだけ使う。対象ThesisとReviewはDBから解決し、local draftは入力にしない。出力は当日時点の助言であり永続化しない。`--target-session`は既存calendarで確認できる次の有効営業日を指定する。引け前は当日、引け後・休日は次の営業日となり、正式判断日と発注日を混同しない。`planned_limit`の期限をそのままbroker報告へ渡し、失効済み・calendar不明の案はdeferする。正式評価日を当日に揃え、利用可能な最新確定quote（寄り前は前営業日）と権利単位を確認する。更新時は`thesis-scaffold --from-thesis-id`で元資料・予測を保持した差分を再Reviewする。価格超過はPlanningのdeferであり企業評価をrejectへ変更しない。既保有、同ticker予約、同CAA買約定済みは追加購入を提案しない。cash不足とguide超過warningを区別する。

同じResearch Setから順次配分できる。CAA-1の人間報告をledgerへ反映した後、同じ調査を参照してCAA-2を公開し、最新の予約控除後cashでPlanningする。1 CAAは1 allocateとし、Operationの完了参照は最後のCAAにする。

Assessmentが`no_allocation / defer`ならPlanning Limit・broker操作へ進まず、人間の見送り判断をOperationに記録する。`allocate`でも当日価格超過や人間のdeferは正常であり、公開済みAssessmentを書き換えず当日の判断を記録する。発注する場合は人間のapproveとbroker操作を待ち、人間が報告したbroker factだけをledgerへ反映する。

## 6. Operation を完了する

`baibai-engine operation checkpoint|complete --payload <FILE>` の `<FILE>` は OperationPayload の YAML / JSON ファイルである。`artifacts` は object の配列、`canonical_refs` は string の配列、`human_confirmation` は `request` / `result` の object、`result` は判断結果の string として記録する。cloud 反映が必要なら `ops-maintenance` に従う。
checkpoint / completeの全置換payloadにも、開始時の`research_triage` artifact（`ref`と`research_set`）を保持する。完了時は`kind: capital_allocation_assessment`と公開済みIDの`ref`を持つartifactを1件含める。serviceは開始時のTriage・Research Setとの一致と公開時刻を検証し、Assessment未公開では完了しない。

YAML では日付・日時に見える scalar が string 以外へ暗黙変換されるため、OperationPayload で JSON string として渡す日付・日時は必ず引用符で囲む。

## 停止条件

次の場合は進めず人間へ返す。

- Research Set または対象範囲が確定していない
- 一次資料で load-bearing claim を確認できず、unknown / defer にも確定できない
- review の独立性または digest binding を満たせない
- buy 判断または当日指値が mandate または価格上限などの必須条件に反する
- 人間の approve 前に broker 操作または ledger 更新へ進もうとしている

資金目安、available cash、concentrationのwarningは人間へ提示し、それだけで投資価値rankを変えたり候補を除外したりしない。受容には[portfolio方針](../../../docs/portfolio-management.md#reservation-and-warnings)とledgerのoverride契約に従う人間確認が必要であり、自動許可しない。ledger書き込み時のcash不足などのhard errorはwarningと区別する。

価格超過、買う価値のある候補なし、一次情報不足を判断できた場合の`defer` / 見送りは正常な結論である。

## 参照

- [`thesis.md`](../../../docs/reference/thesis.md)
- [`capital-allocation-assessment.md`](../../../docs/reference/capital-allocation-assessment.md)
- [Research Playbooks](../../../method/research/playbooks/README.md)
- [`business-model-research.md`](../../../docs/reference/business-model-research.md)
- [`portfolio-management.md`](../../../docs/portfolio-management.md)
- public CLI `--help`
