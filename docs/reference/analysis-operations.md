---
title: "Researchの選定と進行"
summary: "三入口から固定選定・企業評価・提案を完成し、保存入力から再開する。"
doc_type: reference
status: active
---

# Researchの選定と進行

通常の「投資提案して」はAIが分析と候補を比較・選定して提案まで進める。指定銘柄と保有見直しは同じResearchの別scope。選定は人間の採用・注文指示ではなく、候補外や0件も扱う。全候補Triage・旧batch analysis runnerは使用しない。操作は[Research skill](../../.agents/skills/research/SKILL.md)が所有する。

## 明示開始と固定対象

Operationが一回の依頼・選定・唯一の入力snapshotと成果を保持する。requestはtext/scope/指定集合を表し、identityはOperationのresearch-op-UUIDが所有する。対象の順序が実行順であり、容量は保存policyに従う。現在の上限を構造型や経済的資格と扱わない。scopeごとにnew/holdingを分け、既保有のnewを黙って保有見直しへ変換しない。

新開始ではread-only contextを読みAIがdraftを作り、prepareでcanonical sourceと現在保有purposeを検証してsnapshotを一度commitする。snapshotには選択行のnull・field欠落をそのまま保持し、header identity不明を現在値で補完しない。機械分析の不足はInputGapで説明し、企業の否定と区別する。

## 固定入力と時点

`Operation.inputs`が入力の正本。workspaceのmanifestはID・digest・purpose・Research日だけを束縛し、domain-inputsは保存snapshotから純粋に投影する。mutable storeやclockを再読しないため、元store整理後も同じ内容を再生成できる。新しい資料はThesis sourcesへ入り、開始入力を書き換えない。

Research日、機械analysis_as_of/run_at、cutoff対応readerのinput_known_at、実際のThesis/Review/CAAの正式評価日を区別する。input_known_atは全入力の厳密なPIT再現保証ではない。Market Context/Readingの既存readerが現在storeから再計算する意味を保持し、prepareで読んだ値を固定する。当日Researchに前日runを使えるが、前日Thesisの日付だけを改変して当日CAAにしない。

## 成果と被覆

各固定対象はReviewed Thesis、検証済み保有draft、または理由・参照付き未完了で被覆する。未完了は経済的rejectとは別。完成newが一件以上なら、その全件と未完了newの固定範囲からCAAを作る。後でprogressが進んでも保存CAAは変わらず、Planningは比較範囲の一致を再確認する。保有はCAAへ混ぜない。0件・全未完了はCAAを捏造せずresult/nextを残して終了できる。人間の採否をL3完了条件にしない。

## 再開と次の依頼

manifest/Operation ID、またはactiveの同一draftで再開する。scope・対象・理由・source・cutoffを変更せず、別activeや別workspace bindingをforceで上書きしない。file失敗後はcommitされたOperation IDから投影を復元する。forceは投影だけを修復して執筆済みdraftを保持する。再開保証は固定入力の同一再生成であり、現在価格・資本・正式評価の条件不変や提案完成の保証ではない。

追加対象は現在の結果・未完了を確定して次Operationへ進める。同文の本当の再依頼をglobal dedupeしない。旧workspace/selectionはruntimeで継続せず、[一回移行工具](../../tools/cutover/research_selection_v2.py)で現在必要な判断とactive handoffだけを選ぶ。

## 統合時の一回移行

application22をread-onlyのconsistent transactionで読み、新schemaへ選択したThesis4/CAA2/PositionReview3と必要ancestryをpayload/hash不変で移す。全ledger/event/meta/price、task・市場判断・outcomeは維持し、ledger replay前後一致を確認する。旧Operation/Triageは移さず、activeの依頼・対象・既存評価・draftをhandoffとして保存する。欠けた理由を過去の判断として補完せず、通常context→prepareで再選定する。約定価格を当時の提案条件へ転記しない。

`uv run python -m tools.cutover.research_selection_v2 --source <OLD_COPY> --output <NEW_COPY> --selection <KEEP_MANIFEST> --workspace-root <ACTIVE_WORKSPACE_ROOT> --check`で不足と閉包を確認し、manifestと指定rootを確認してから新未存在outputへ実行する。activeがある場合は工具専用`--workspace-root`が必要であり、そのroot内のmanifestが旧Operation IDと一致するworkspaceだけを非破壊退避する。root外の参照・不一致・不足は報告して停止し、通常runtimeに探索fallbackを置かない。最終domain_v1工具へ同じ引数を統合し、親が実パスを指定する。合成fixtureと隔離コピーで検証する。A工具はドメイン全体の最終移行の構成要素であり、A単独の本番切替を前提にしない。

統合releaseの切替窓はapplication全writerとactive draft編集を停止し、引継先を確認する。最終copy後に新read transactionでsourceの全保持table内容とledger末尾を照合し、変化があれば切り替えずcopyをやり直す。code/storeは最終schemaが一致した世代で親が順序を判断する。source/backupを不可逆削除せず、日次batchに移行を代行させない。
