---
title: "Researchの選定と進行"
summary: "三入口から固定選定・企業評価・提案を完成し、保存入力から再開する。"
doc_type: reference
status: active
---

# Researchの選定と進行

通常の「投資提案して」はAIが分析と候補を比較・選定して提案まで進める。指定銘柄と保有見直しは同じResearchの別scope。選定は人間の採用・注文指示ではなく、候補外や0件も扱う。全候補Triage・旧batch analysis runnerは使用しない。操作は[Research skill](../../.agents/skills/research/SKILL.md)が所有する。

## 明示開始と固定対象

Operationが一回の依頼・選定・唯一の入力snapshotと成果を保持する。requestはtext/scope/指定集合を表し、identityはOperationが発行するresearch-op identityが所有する。対象の順序が実行順であり、容量は保存policyに従う。現在の上限を構造型や経済的資格と扱わない。scopeごとにnew/holdingを分け、既保有のnewを黙って保有見直しへ変換しない。

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

application22をread-onlyのconsistent transactionで読み、最終application24へ一回で切り替える。選択した旧評価・提案・保有理由と必要な根拠を執筆材料draftへ抽出し、新Assessment/Proposal/Operationとして自動公開しない。台帳全event/meta/price、task・市場判断・outcomeを保持し、ledger replay前後一致を確認する。旧買付判断はticker識別の最小bindingを保持し、未保存の価格・数量・期限を約定から作らない。

```bash
uv run python -m tools.cutover.domain_v1 \
  --source-app <OLD_APP_COPY> --output-app <NEW_APP_COPY> \
  --source-runs <OLD_RUN_COPY> --output-runs <NEW_RUN_COPY> \
  --manifest <KEEP_MANIFEST> --draft-dir <MATERIAL_DRAFT_DIR> \
  --workspace-root <ACTIVE_WORKSPACE_ROOT> --check
```

--checkは書込なしで不足と必要参照を報告する。実行は新未存在output/draft-dirへ限り、source/backupを保持する。activeがある場合は工具専用workspace-rootが必要で、旧Operation IDに一致するworkspaceだけを非破壊退避する。root外・別Operationを黙って探索しない。通常runtimeに旧workspaceのfallbackを置かない。

旧run cacheは原本または必要な選択根拠を退避し、新runは通常Screeningで実時刻・新IDにより再生成する。切替後は通常selection-context→prepareで新Operationを開始し、退避材料を確認して新Assessment/独立Reviewを公開し、そのOperationと現在入力から新Proposalを作る。新成果を移行manifestへ事前登録しない。欠けた理由・評価を過去の判断として補完せず、Fillから旧提案条件を復元しない。工具の検証と隔離コピーによるrehearsalを、本番切替実施と混同しない。

統合releaseの切替窓はapplication全writerとactive draft編集を停止し、引継先を確認する。最終copy後に新read transactionでsourceの全保持table内容とledger末尾を照合し、変化があれば切り替えずcopyをやり直す。code/storeは最終schemaが一致した世代で親が順序を判断する。source/backupを不可逆削除せず、日次batchに移行を代行させない。
