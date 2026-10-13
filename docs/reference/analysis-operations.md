---
title: "Research進行とTriage runner"
summary: "明示開始、有限対象、部分完了と提案成果、Triage runnerの境界。"
doc_type: reference
status: active
---

# Research進行とTriage runner

`baibai-batch analysis run`は、保存済みの対象日Review Setを一つのAI判断へ渡し、engineのserviceを通してTriageを発行する。Screening Run・Review Setの生成、macro取得、cloud公開は行わない。操作の順序は[Research Triage skill](../../.agents/skills/research-triage/SKILL.md)が所有する。

<a id="research-triage-contract"></a>

## Research Triageの契約と発行

`research/triage`が型・query・service・CLIを所有する。manual入口は`research triage scaffold / publish`であり、ScreeningにL3 writerを置かない。

application DBのcurrent `research_triage`はReview Set全entryをexactly onceで保持する。
current writerとactive read pathはcurrent schemaだけを扱い、retired shapeを補完・別名投影しない。

- `research`: contiguousな`priority`、`rationale`、`research_question`、`key_risk`が必須
- `skip`: `rationale`が必須で、`priority`、`research_question`、`key_risk`は禁止

manual scaffoldはReview Set IDからrun storeのcanonical publicationを解決し、入力確定時刻known_atまでに発行された、Review Setのas-of以下で最新の市場環境判断と、application DBのlatest current Triage IDを取得する。通常の`analysis run`はeditable scaffoldを介さず、strictなjudgment fieldから同じdomain objectを組み立てる。publisherは`review_set_id`、`run_revision_id`、`as_of`、`screening_rules_hash`、Candidate Discovery Method、全tickerを検証し、Review Set Entry snapshotをsource Review Setから上書きする。persisted field `candidate_snapshot`はstorage contractとして維持し、snapshotはidentity、`nominations`、grouped `analysis`だけを持つ。

発行は最新Triageを期待するCASで競合を検出し、as-ofと発行時刻の後退を拒否する。同じID・同じcanonical payloadの再送は冪等であり、同じpriorから分岐した別draftは発行できない。保存済み読取・payload hash・実時刻でのhead順序は[query](../../engine/src/baibai_engine/research/triage/query.py)、CAS transactionは[ResearchTriageService](../../engine/src/baibai_engine/research/triage/service.py)、payloadの厳密な条件は[ResearchTriage model](../../engine/src/baibai_engine/research/triage/contracts.py)が所有する。

市場環境判断が無ければ`null`は正常、古ければwarningであり、どちらもReview Setのnomination、membership、orderを変えない。参照は任意であり、指定した場合は`as_of`以下かつ入力確定時刻`known_at`までに発行された保存済みrevisionを検証する。`analysis run`はこの時刻を入力から発行確認まで固定し、`--known-at`で過去cutoffを指定できる。manual発行でcutoffが無い場合はTriageの発行時刻を使う。明示的に古いeligible revisionを選ぶ場合は選択理由を判断文へ残す。発行後payloadと既存runnerの`input.json`はimmutableである。

ADVは固定floorのgateや自動skip条件ではない。Triageは低値・欠損だけで`skip`やpriorityを決めず、価値仮説を比較した後の実行可能性contextとしてrationaleへ反映できる。最終的な注文可否と数量はCapital Allocation後のhuman executionが所有する。

Research Triageは新規のadmission可能範囲を定め、対象選択そのものや購入を認定しない。明示開始後の有限選択・部分完了・保有との合算は[Research進行](./analysis-operations.md#明示開始と固定対象)、操作は[Research skill](../../.agents/skills/research/SKILL.md)が所有する。

E[r] calibration contextは共有read contractがartifact schema、generated/expiry、3y/5y、quantile/band、rules hash、E[r] model versionを検証する。ResearchはTriage rootのrules hashとReview Set Entryの`analysis.expected_return`のmodel/version・ratio-valued `er_annual`を使い、利用不能理由を明示する。これはhistorical contextで、membership/order、Triage、FV、buy judgmentへ伝播しない。

## 明示開始と固定対象

ユーザーの開始要求を記録し、AIが新規・保有を合わせて最大3銘柄、0件も含む固定集合を選ぶ。Operationの`research_selection` artifactがticker・purpose・開始要求・新規Triage参照を所有する。再開・workspaceの`--force`で対象を追加・補充せず、同じ集合・Triage・分析対象日なら元のOperationを継続する。workspaceのas-ofはOperation.as_ofへ一致させ、再開で時点をずらさない。正式なThesis評価日や引用資料の日付はこの分析対象日と区別する。新規はTriage admission、保有は現在ledgerを検証し、保有をCAAの新規集合へ混ぜない。0件はOperationに提案なしの終了を残し、架空のCAAを作らない。

`research prepare`は新規workspaceと、選んだ保有の`holdings/<ticker>` workspaceを同じOperationへ束縛する。`position-prepare`単独も明示開始要求から保有Operationを作る。manifestのOperation IDとselection hashは対象の付け替えを検出し、completed Operationへ新しい調査を足さない。旧manifestのOperation ID欠如は履歴statusで読めるが、scaffold/review/promoteのmutation入口としては認めず、明示prepareによる束縛が必要である。

保存済み三領域を`research/inputs.py`で合成した`domain-inputs.yaml`は調査の入力である。機械anchor、個別/市場/業種、macro Reading、外部提示値を意味ごとに保持し、欠測を新規取得やAI起動で埋めない。既存Market Context Assessmentは任意の参照であり、毎回独立した全depth reportや主観確率を作る前提にしない。重要な環境上の含意だけを企業評価・提案のsource、仮定、反対仮説へ接続する。

## 成果の被覆とL3完了

`research_progress` artifactは対象ごとの完了成果または調査未完了の理由・参照を保持する。新規はcanonical Reviewed ThesisのID/core hash/Review ID、保有は既存PositionReviewの検証済みdraft・検証時刻・Thesis参照と数量/quoteを残す。未完了を企業reject、経済的見送り、no_allocationへ変換しない。

CAAは[完了した新規対象と未完了範囲](./capital-allocation-assessment.md)へ固定される。新規Aが完了、Bが未完ならAだけを比較できるが、Bの理由・参照を隠さない。単なるsubset許可ではなく、新規選択集合を完了＋未完了で重複なく覆う。Operation全体の完了時は保有を含む選択集合も被覆する。全件未完了・0件ならCAAなしで終了する。

L3の提案完成はOperation `completed`であり、人間採否・注文・約定は別工程である。新規完了対象があればその比較範囲の公開済みCAAを参照するが、`allocate / defer / no_allocation`のいずれも人間裁定を完了の必須にしない。保有の検証済み提案は確認済みPosition Reviewとして発行せず、採用時に現行の`--confirmed` publishで入力を再検証する。検証時点の提案が保存された後の時間経過や別対象の作業で、その提案完成の記録を失わない。実注文・約定・ledgerは人間が報告した事実だけを記録する。

厳密なfieldと検証は`research/operation/{progress,validation,service}`が所有し、操作列は[Research](../../.agents/skills/research/SKILL.md)・[Position Review](../../.agents/skills/position-review/SKILL.md)が所有する。

## 既存履歴とactiveの継続

完了済みOperation、Thesis/Review、確認済みPosition Review、旧CAAを再生成しない。既存activeの新契約への束縛は、明示開始要求があり、元のticker集合と新規Triage参照を保ち上限3以内の場合だけ。旧人間選択の記録は履歴artifactとして保持し、今回の最終採否とはしない。対象不明・上限超過・集合変更は停止し、自動closeや対象切捨てを行わない。これは既存activeの限定した継続であり、旧仕様の並行writerは持たない。

## 入力と判断の境界

`--asof YYYY-MM-DD`は対象日の最新Review Setを使う。option省略は今日JSTのexact指定であり、対象日になければ`no_review_set`（exit 0）となり、別日へfallbackしない。

`--latest`はpull済みruns storeの最新as-of・同日最新canonical publicationを既存resolverで選ぶ。`--asof`とは排他である。最新Screening RunとReview Setのrevision一致とpayloadのidentity bindingを確認し、同じexact Review Setの既存Triage・Market Context Assessmentを結合する。Review SetまたはRunがない、revisionが不一致、payloadやbindingが不正な場合は`failed`（non-zero）となり、model起動・canonical writeは0。新しい日や同日の後続revisionが未発行でも古いReview Setへfallbackしない。calendar上の未実行判定は行わず、同期済みstoreの整合を確認する。

Review Set全件、[TRIAGE_POLICY](../../batch/src/baibai_batch/analysis/policy.py)、利用可能な市場環境判断の根拠・含意・反証/不確実性を含む共有projectionを一つのpayloadとして渡す。AIにrepository・runbook・成功logを読ませず、ID・CAS・publish操作を判断させない。

Triageの対象集合と保存bindingはengineが検証する。候補を`research / skip`へ分類し、調査優先度を決めるもので、購入やOperation開始ではない。active OperationがあってもTriageは実行できる。

## 終了状態

| status | 意味 | model起動 |
| --- | --- | --- |
| `already_running` | local analysisのlockを取得できない | 0 |
| `no_review_set` | 対象日のReview Setがない | 0 |
| `empty_review_set` | 対象Review Setのentryが0件 | 0 |
| `research_available` | exact Review SetのTriageが既存で、research候補がある | 0 |
| `already_published` | exact Review SetのTriageが既存で、全skip | 0 |
| `published_research_available` | 今回の発行経路が成功し、research候補がある | 通常1 |
| `published_all_skip` | 今回の発行経路が成功し、全skip | 通常1 |
| `failed` | 入力・AI・binding・write等の失敗 | 発生地点による |

正常状態はexit 0、失敗はnon-zeroである。非営業日であること自体はno-op条件ではない。正常な対象なしと、保存物の破損を区別する。

## 結果と再実行

成功時はcanonical Triage ID、as-of、priority順の全research候補を返す。新規の有限選択はこのexact IDへ結び付ける。選択と継続はユーザーの明示開始を前提とし、Triage発行だけでAI調査を起動しない。

実行結果と必要な診断は`${XDG_STATE_HOME:-~/.local/state}/baibai-loop/analysis/<timestamp>/`の`summary.json`、`run.log`等にある。通常成功時のlog再読は不要である。機密を含み得るadapter出力はredactionされたprivate logとして扱う。

表示やlog保存が失敗しても、canonical writeの有無を終了コードだけで決めない。原因を解消して同じ対象日をfreshに実行すると、既存のexact Review Set照合がTriageの再生成を避ける。旧workspaceのresumeや、別日のlatestによる補完はしない。
