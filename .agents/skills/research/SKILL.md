---
name: research
description: 人間が選んだ候補を一次情報で深掘りし、thesis、独立反証、buy または見送りの統合判断まで確定する。候補の提示までは research-triage skill。
---

# Research

## 目的

Research Triage から人間が選んだ候補を一次情報で検証し、buy または理由付きの見送りへ確定する。broker 操作と発注は人間が行う。

`normalized_per_3fy`はNormalized Earnings Powerのnative eligibility/order座標であり、ResearchのFV/E[r] estimator入力へ転用しない。

## 開始条件

- canonical Research Triageをartifact / canonical refに持つactiveな`capital-allocation` Operationと、人間が確認したResearch Setがある。同じ`as_of`だけの別Operationを採用せず、Research用に別sessionを開始しない。
- 依頼が subset を指定した場合は、その範囲だけを扱う。
- 開始時の人間確認を operation checkpoint に記録する。

## 1. Workspace を準備する

```bash
uv run baibai-engine research prepare \
  --research-triage-id <RESEARCH_TRIAGE_ID> \
  --db stores/application/baibai.sqlite \
  --workspace .cache/research/<ASOF>
```

as-of、20件の比較snapshot、Researchへ進められるtickerはapplication DBのpublished Research Triage v2から導出する。Review Set fileやrun storeはResearch開始後のauthorityではない。生成された`research-workspace.yaml`の`research_set`には、人間が選んだtickerのうちTriageが`research`としたものだけを書く。

## 2. Case ごとの thesis を確定する

各 case で次を行う。

1. `research thesis-scaffold` で thesis を作る。
2. 会社 IR、EDINET、決算資料などの一次資料で load-bearing claim を調べる。検索 snippet、二次情報、外部 AI 出力を観測事実にしない。playbook は `applies_to_valuation_approach_ids` の明示 mapping だけを使い、同名 slug から implicitに対応を推測しない。[事業モデル別リサーチ](../../../docs/reference/business-model-research.md)は指定 playbook の補助に限る。
3. checklist は [Research Playbooks](../../../method/research/playbooks/README.md#work-state) の作業状態として更新する。証拠が得られなくても調査が終わり、unknown / defer を記録した項目は `complete` であり、verified とは書かない。
4. Research Triage に束縛された Macro Context を開き、scenario arithmetic と FV の前に `connection.estimate_caveats` を確認する。対象企業・評価法に material な caveat は既存 scenario assumption の文章と `source_ids` へ接続する。適用外、stale、または low materiality なら、その理由を `screening_fv_bridge.note` に残す。新しい macro field は足さない。そのうえで seven axes、countercase を埋める。macro と E[r] は context であり単独 gate にしない。AIを含む技術・産業構造変化も、materialな場合だけ通常Researchの既存scenario、FV、risk、countercase、assessmentへ接続し、専用checkを作らない。
5. `research evaluate` を実行し、`buy` で review が未作成の場合の review 要求を除く error を 0 にする。
6. thesis が安定してから `research review-scaffold` を作り、独立した反証役が review する。独立 review は、束縛 Context の material な estimate caveat が scenario assumptionへ接続されたか、または適用外 / stale / low materiality の理由が既存 note にあるかを反証する。thesis を変えたら `--force` で review を再生成し、core hash を更新する。

## 3. 比較して disposition を決める

全 case を `buy` / `defer` / `reject` まで進め、FV、5 年 CAGR、countercase、disposition を横断比較する。review 後に `research promote` で全 case を canonical にし、`research status` が示す `next_command` に従って未完了 case を残さない。

## 4. Buy case の当日指値を確認する

`research plan-limit` は canonical Capital Allocation Assessment が `allocate` の alternative にだけ使う。出力は当日の助言であり永続化しない。evidence gap が残る場合は、人間の override と `sizing_action: reduced` を両方記録し、1 board lotでも大きすぎる場合は `defer` に戻す。要求利回り未達または永久損失結論が elevated の case はサイズを縮めて買わない。価格が max buy price を超えた通常状態は `defer` とする。

## 5. Assessment と独立 review を公開する

`research capital-allocation-scaffold` で promote 済みの全 case を Capital Allocation Assessment に含め、`disposition_reason` に具体的な判断理由を書く。research question が複数論点を含む場合は分割し、一部未解決のまま全体を `answered` にしない。

1. `research capital-allocation-publish --check` で digest を確認する。review 前の `review_binding=stale` は正常。
2. Capital Allocation Assessment author と別の役が独立 review を作る。
3. content digest が一致してから assessment と review を publish する。
4. deferred monitoring を task にする場合は、既存 task と重複しないことを確認して dated task を 1 件だけ作る。

## 6. Operation を完了する

`baibai-engine operation checkpoint|complete --payload <FILE>` の `<FILE>` は OperationPayload の YAML / JSON ファイルである。`artifacts` は object の配列、`canonical_refs` は string の配列、`human_confirmation` は `request` / `result` の object、`result` は判断結果の string として記録する。cloud 反映が必要なら `ops-maintenance` に従う。
YAML では日付・日時に見える scalar が string 以外へ暗黙変換されるため、OperationPayload で JSON string として渡す日付・日時は必ず引用符で囲む。

## 停止条件

次の場合は進めず人間へ返す。

- Research Set または対象範囲が確定していない
- 一次資料で load-bearing claim を確認できず、unknown / defer にも確定できない
- review の独立性または digest binding を満たせない
- buy 判断または当日指値が mandate、資金、concentration 制約に反する
- 人間の approve 前に broker 操作または ledger 更新へ進もうとしている

## 参照

- [`thesis.md`](../../../docs/reference/thesis.md)
- [`capital-allocation-assessment.md`](../../../docs/reference/capital-allocation-assessment.md)
- [Research Playbooks](../../../method/research/playbooks/README.md)
- [`business-model-research.md`](../../../docs/reference/business-model-research.md)
- [`portfolio-management.md`](../../../docs/portfolio-management.md)
- public CLI `--help`
