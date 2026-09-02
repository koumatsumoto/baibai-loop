---
title: "Daily analysis token optimization baseline and implementation evidence"
summary: "Issue #1172の運用surfaceとIssue #1183の固定20候補実測によるhistorical evidence。"
doc_type: historical-evidence
status: completed
as_of: 2026-09-02
---

# Daily analysis token optimization baseline and implementation evidence

> **Historical scope:** この文書は各Issue時点の実装・計測証拠であり、現行のdaily analysis契約や再現手順ではない。現行契約は[`analysis-operations.md`](../../../docs/reference/analysis-operations.md)を参照する。

## 結論

`main@757753d5bcdc5deae47a83fc6ededc05cd962455`では、Research Triage / Macro Context skillを起動した後にno-opを判断するため、model process起動前に`no_ai`を確定する経路は無かった。変更後はmachine dispatcherがpacketを先に作り、task 0件と全task exact reuseでmodel invocation 0を機械契約にした。1candidateのsemantic inputだけが変わるfixtureでは、その1taskだけが`reused=false`になる。

historical provider usage、実input/output token、AI wall timeは従来保存していなかったため、変更前10 runの実token P50は復元不能である。品質を落として70%削減を捏造せず、model起動数・packet bytes・estimated tokenを再現可能なfixtureで比較し、merge後の実運用usage測定をsuccessorで行う。

## 基準と測定方法

- baseline code / skills: `main@757753d5bcdc5deae47a83fc6ededc05cd962455`
- after: Issue #1172 implementation tree
- deterministic evidence: `tests/batch/test_analysis_ops.py`と`tests/batch/test_cloud_daily_batch.py`
- fast reproduction: 当時の`tools/verification/run_analysis_ops.py --profile fast`（現行treeでは削除済み）
- runtime metrics: workspaceの`metrics.json`

tokenはprovider usageが得られる場合だけactualを記録する。得られない場合はUTF-8 packet bytesを4で割った明示的概算を使う。raw log、skill全文、CLI helpはafter packet bytesへ含めない。

## 10ケース

| case | baseline machine / model境界 | after acceptance | fixture |
| --- | --- | --- | --- |
| normal unchanged day | skill起動後に現況確認 | exact cacheならmodel 0 | all reused |
| candidate一部変化 | Review Set全件を再読 | changed taskだけmodelへ渡す | 1/2 changed |
| candidate大幅変化 | Review Set全件を再読 | changed task集合だけを渡す | digest table |
| macro no trigger | skill起動後にreading要否判断 | macro task 0、model 0 | no `--macro-review` |
| macro trigger | full context手順を読む | isolated current phase task 1 | manual trigger |
| non-business day | dailyの後に人間がskip確認 | `no_ai`、model 0 | daily skip contract |
| partial-quality | stdoutからrun identityを転記 | exact manifestにidentity固定 | daily exit 2 fixture |
| deferred macro | full workflow logから原因探索 | business result維持、reason/log path | daily exit 3 fixture |
| interrupted / resume | 手順とstdoutから状態復元 | exact active pointer / fingerprint、unsafe rediscovery拒否 | workspace state tests |
| failure investigation | 成功時を含むfull stdoutを読む | status → reason → bounded tail | log contract tests |

## 観測値

| metric | baseline | after |
| --- | --- | --- |
| no-ai / exact reuse model invocation | model起動前判定なし | 0 |
| one changed candidate |全candidateを同じskill contextで処理 | 1 task |
| success raw log read | command stdoutが主観測面 | 0 |
| operation / run / Review Set binding | stdout /一時fileから転記 | exact manifest field |
| task packet size | bounded契約なし | 96,000 bytes / task、64,000 bytes / index hard cap |
| per-step log size | bounded契約なし | streamごと2,000,000 bytes cap + truncation metadata |
| actual provider token P50 | 未保存、復元不能 | `metrics.json`に取得可能時だけ保存 |

skill本文の長さはtoken成果の代理にせず、model processを起動しない判定、stable batching、task payloadの限定を主要成果とする。Macro Contextのfull-depth source確認・独立反証は削減対象外である。

## canonical equivalenceと未達

dailyの工程順、screening exit 2、deferred exit 3、Review Set publisher、Research Triage publisher、human Research Set gateは変更していない。structured outputとhuman outputは同じ`DailyBatchResult`から生成し、volatile ID/time以外のidentityとexit semanticsを同じfixtureで検証する。

実運用10 runのactual token P50 70%削減は、baseline usageが存在しないため本reportでは証明していない。merge後は`metrics.json`のmodel invocation / packet bytes / actual usage availabilityを10ケースで採り、未達でもevidenceや独立反証を削らず残存costを分解する。

## Issue #1183 固定20候補の実測

`main@dc69f952295dd5063d585393586a11c06e5c3e21`を基点とするIssue #1183実装treeで、canonical storeへ書かない固定20候補fixtureを現在の`codex exec`へ1回渡した。Review Set相当の全候補を1 requestにまとめ、AIのtool callは許可していない。

| metric | observed |
| --- | ---: |
| candidate count | 20 |
| model input + output schema | 32,753 bytes |
| model requests | 1 |
| actual input tokens | 27,424 |
| actual output tokens | 4,282 |
| AI duration | 119.831837 seconds |
| tool calls | 0 |
| decisions | 20 |

測定runtimeは`codex-cli 0.152.1`である。business payloadとschemaのbyte量だけではactual input tokenを説明できず、fixed overheadが残ることは確認できた。一方、1回のfixtureだけでは現在のtoken使用量が運用上の問題だとは判定できないため、別provider adapter、cache、queue、direct API wrapperは追加しない。

既存のlocal `analysis run` artifactは無かったため、同じ`export-read-models` commandをcurrent canonical storeへのread-only入力とtemporary outputで1回測定した。exit 0、3,851 files、20.406288 secondsだった。end-to-end dailyに占める割合をこの単独測定から判定できないため、optional materialize skipは追加しない。
