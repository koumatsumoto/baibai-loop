---
title: "Researchの選定と進行"
summary: "三入口から固定選定・企業評価・提案を完成し、保存入力から再開する。"
doc_type: reference
status: active
---

# Researchの選定と進行

通常の「投資提案して」はAIが分析と候補を比較・選定して提案まで進める。指定銘柄と保有見直しは同じResearchの別scope。選定は人間の採用・注文指示ではなく、候補外や0件も扱う。全候補Triage・旧batch analysis runnerは使用しない。操作は[Research skill](../../.agents/skills/research/SKILL.md)が所有する。

## 明示開始と固定対象

Operationが一回の依頼・選定・唯一の入力snapshotと成果を保持する。requestはtext/scope/指定集合を表し、identityはOperationが発行するresearch-op identityが所有する。対象の順序が実行順であり、容量は保存policyに従う。現行policyは最大3対象で、定義は[`selection_policy.py`](../../engine/src/baibai_engine/research/selection_policy.py)が所有する。現在の上限を構造型や経済的資格と扱わない。scopeごとにnew/holdingを分け、既保有のnewを黙って保有見直しへ変換しない。

新開始ではread-only contextを読みAIがdraftを作り、prepareでcanonical sourceと現在保有purposeを検証してsnapshotを一度commitする。snapshotには選択行のnull・field欠落をそのまま保持し、header identity不明を現在値で補完しない。機械分析の不足はInputGapで説明し、企業の否定と区別する。

## 固定入力と時点

`Operation.inputs`が入力の正本。workspaceのmanifestはID・digest・purpose・Research日だけを束縛し、domain-inputsは保存snapshotから純粋に投影する。mutable storeやclockを再読しないため、元store整理後も同じ内容を再生成できる。新しい資料はStock Assessment sourcesへ入り、開始入力を書き換えない。

Research日、機械analysis_as_of/run_at、cutoff対応readerのinput_known_at、実際のAssessment/Review/Proposalの正式評価日を区別する。input_known_atは全入力の厳密なPIT再現保証ではない。Market Context/Readingの既存readerが現在storeから再計算する意味を保持し、prepareで読んだ値を固定する。当日Researchに前日runを使えるが、前日Assessmentの日付だけを改変して当日Proposalにしない。

## 成果と被覆

new/holdingの各固定対象はexact Reviewed Assessment参照または理由・参照付き未完了で被覆する。未完了は経済的unsupported/no_actionと別に扱う。全対象の被覆から共通Investment Proposalを作り、未調査は評価参照なしのdeferとして保存する。0対象では空Proposalを捏造せず選定理由とresult/nextを残す。

Operationは公開済みProposalのID/hashを保持し、本文やhold draftを再保存しない。Proposal発行時には現在Operationと固定scopeを照合するが、後でprogressが進んでも保存scopeを書き換えず、通常read/previewに強制一致を要求しない。具体termsが足りないbuy/sellは未完了理由を明示し、具体提案完成と区別する。人間の採否をL3完了条件にしない。

## 再開と次の依頼

manifest/Operation ID、またはactiveの同一draftで再開する。scope・対象・理由・source・cutoffを変更せず、別activeや別workspace bindingをforceで上書きしない。file失敗後はcommitされたOperation IDから投影を復元する。forceは投影だけを修復して執筆済みdraftを保持する。再開保証は固定入力の同一再生成であり、現在価格・資本・正式評価の条件不変や提案完成の保証ではない。

追加対象は現在の結果・未完了を確定して次Operationへ進める。同文の本当の再依頼をglobal dedupeしない。旧workspace/selectionはruntimeで継続せず、[一回移行工具](../../tools/cutover/domain_v1.py)で現在必要な材料とactive handoffを退避する。

## 統合時の一回移行

保存材料の引継ぎ・writer停止・隔離検証・本番切替は[Research domainの一回移行](../../batch/OPERATIONS.md#research-domain-cutover)に集約する。通常Researchの開始・再開と、旧schemaからの一度限りの切替を混同しない。
