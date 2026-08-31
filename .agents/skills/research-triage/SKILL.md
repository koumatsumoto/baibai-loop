---
name: research-triage
description: screening runからReview Setを発行し、全entryをresearch / skipへ分類して、人間がResearch Setを確定するまで進める。深掘りはresearch skill。
---

# Research Triage

4つの価値評価法から有限のReview Setを作り、Fundamental Researchの時間を使う価値がある対象を判断する。Researchは買い推奨ではなく、Skipも正常な結論である。AIはbroker操作へ進まない。

## 前提

1. AGENTS.mdに従い`capital-allocation` sessionを開始または再開する。
2. canonical ledgerと必要storeを確認する。同期が必要なら`ops-maintenance` skillに従う。
3. `git status --short --branch`、public `--help`、coverageを確認し、入力矛盾では停止する。

## 手順

1. 最新完全営業日を`ASOF`として入力を準備する。

   ```bash
   uv run baibai-engine screening verify-cache-coverage --asof <ASOF>
   uv run baibai-engine screening extract-edinet-metrics --asof <ASOF>
   uv run baibai-engine screening run --asof <ASOF>
   ```

   coverage不足時だけ`bootstrap-cache`を使う。通常運用で`--allow-stale-jpx`を使わない。

2. runが返したimmutable revisionからReview Setを1回だけ発行する。

   ```bash
   uv run baibai-engine screening review-set publish --asof <ASOF> \
     --run-revision-id <RUN_REVISION_ID> \
     --output-path <workdir>/review-set.yaml
   ```

   再表示は`screening review-set show --review-set-id <ID>`を使う。E[r]・FV・macro・portfolio stateは参考文脈であり、entryの採否や順序を変えない。

3. Review Setの出力から、評価法座標を転記済みのfail-closed draftを作り、全entryを`research`または`skip`へ分類する。

   ```bash
   uv run baibai-engine screening research-triage scaffold <review-set.yaml> \
     --db stores/application/baibai.sqlite \
     --output-path <workdir>/research-triage.yaml
   ```

   手書きで全ticker・評価法を転記しない。出力の`decision`と`TODO`散文はpublish前にすべて置換する。構造の参照は[`assets/draft-template.yaml`](./assets/draft-template.yaml)を使う。

   scaffold は Review Set の as-of 以下で最新の Macro Context を `macro_context_id` へ自動束縛する。非 `null` なら、draft の ID を使って Context を読む。

   ```bash
   uv run baibai-engine macro context --db stores/application/baibai.sqlite show \
     --context-id <MACRO_CONTEXT_ID> --asof <ASOF>
   ```

   connection のうち各 entry に実際に該当する `research_priority_hints`、`bargain_topography`、`sizing_cautions` だけを既存の `rationale` / `research_question` / `key_risk` へ接続する。macro prose を全 entry へ一律に複写せず、Review Set の nomination・membership・orderも変えない。

   - `research`: contiguousな`priority`、具体的な`rationale`、`research_question`、`key_risk`を必須とする。
   - `skip`: `priority`、`research_question`、`key_risk`を持たず、具体的な`rationale`を必須とする。
   - `null`や`unknown`を否定事実へ変換しない。
   - economic factの`unknown` / stale / data-quality warningはsystem failureではない。調査価値があるなら`research_question`または`key_risk`へ渡し、unknownだけで`skip`を強制しない。
   - 原則はReview Setのmachine facts / contextで判断する。Research時間を使うかだけを安価に決める限定された1事実（現在のTOB・上場状態、直近開示で仮説が既に消滅したか等）は一次資料で確認できるが、正常利益、FV、scenario、business model、permanent lossの分析へ展開しない。限定確認後も不明ならunknownとしてResearchへ渡せる。
   - E[r]はestimateとしてのみ読み、個別予測やResearch判断の自動gateにしない。
   - rationaleで評価法を名指す場合は、同じentryの`machine_snapshot.nominations`と一致させる。

4. canonical Research Triageを発行する。

   ```bash
   uv run baibai-engine screening research-triage publish <draft> \
     --db stores/application/baibai.sqlite --runs-db stores/screening/runs.sqlite
   ```

   publisherがReview SetとのID、run revision、as-of、全ticker一致、Review Basisに加え、Macro Context の存在と未来参照をfail-closeで検証し、機械座標をsnapshotへ焼き込む。as-of 以下の Context があるのに `null` は許さない。最新より古い eligible Context を明示選択する場合は、選択理由を該当 entry の既存判断文へ書く。Context が無いことは正常な `null`、古いことは warning であり、どちらも候補選定の gate にしない。

5. `research` entryを人間へ提示し、人間がその部分集合をResearch Setとして確定する。確定結果をoperation checkpointへ記録する。0件なら`completion_reason: no-research`でsessionを完了できる。

## 停止条件

- operation、ledger、store、Review Basisに矛盾がある
- coverageがfuture、required storeがunreadable / corrupt、またはReview Set・run・as-of・Review Basis・machine snapshotのbindingが成立しない
- 人間がResearch Setを確定していないのにresearchまたはbroker操作へ進もうとしている

## 正本

- [`screening-runtime.md`](../../../docs/reference/screening-runtime.md)
- [`valuation-metrics.md`](../../../docs/reference/valuation-metrics.md)
- [`portfolio-management.md`](../../../docs/portfolio-management.md)
- public CLI `--help`
