---
title: "External Assessmentの永続名切替: 隔離検証"
summary: "schema30コピーの全列保全と旧固定graph・新名releaseの読戻し。本番未反映。"
doc_type: report
status: historical
---

# 隔離検証

基準mainは`56298132aa860e0fc722965dd8f1d82d7867a287`（#1437）。cohort stage簡素化後のpolicy v6、stage/raw bundleを用い、3 table/index/dataset・新規plan/terminal・coordination/raw prefix・workflow/concurrencyをExternal Assessment名へ統一する。共有publication lease、他領域のdataset contract、Market Contextの物理名・判断payloadは変更していない。切替正本は[batch運用](../../../batch/OPERATIONS.md#external-assessment-identity-cutover)。

## 保存コピーの比較

以前の本番読戻しで保全したschema30コピーをreadonly入力とし、別fileへschema31を作成した。元DBの全bytes hashは処理前後で一致。3観測tableと他の全tableについてPK順の全列digest・行数を照合し、全件一致した。元受領UTCの文字列、JST基準日、表示更新表記、出所・parser履歴、0/NULLを変更していない。3状態のExternal Assessment全出力は旧読取commit `04e6845d7455aaf9b1780319ad84a2600d55ff40` と同じcutoffで一致した。

旧固定releaseの不足manifest metadataだけを通常のread-only経路で取得し、保存済みmanifest hashを検証した。既存Parquetを再取得したりproviderへ再接続したりしていない。旧graphはmanifestに記録されたproducer commit `64812a767052b674e3a37ee8f1a588f51cf52e47`で読み、保存コピーの全typed履歴digestと一致した。

実装commit `d17af5e725704c22c6e85a61840da087b1ddd48d`で同じschema31コピーをsealed exportし、新名datasetの全履歴releaseをoffline生成した。空cacheから読戻したtyped履歴の全列digestは新DBと一致。旧graphの既存file bytesは不変で、準備処理はcurrent pointerへ書いていない。Research入力のExternal Assessmentは3状態とも保存queryと一致し、読取したapplication DBの全bytes hashも不変だった。比較payload・DB・生値・保存先は公開しない。

## fixtureと実コピーの範囲

実コピーは以前保全した世代であり、最新本番の全件取得結果とは扱わない。この世代にはQuantsが未収録のため、Quantsの実値保全を証明したとはしない。3媒体の合成fixtureでは異なる月の履歴、英字ticker、0・正常NULL・人数0、日本語の元表示、microsecond付きUTCを用い、3 tableの改名・新index・全列保持・固定releaseからのhydrate・再投入0行を検証した。

旧名contractの合成graphから新releaseを準備し、coverage floor対応、旧graph不変、pointer非作成、別origin拒否も検証。旧schema読取の拒否、出力先上書き拒否、壊れたschemaで部分migration fileを残さないことを確認した。旧名のruntime alias・dual writer・旧plan rebindingはない。

## local gate

- Python: 3,883 passed / 8 skipped、coverage 86.05%。skipは専用R2 acceptance credentials未設定。
- Ruff format/lint、mypy 449 files、33 import contracts、全drift gate、Bandit、runtime/dev・build依存監査成功。
- UI: 262 passed、lint/build/audit成功。Worker: 88 passed、types/typecheck・deploy dry-run・audit成功。
- 全回帰がPositionの独立market schema guardを検出し、30→31へ追従させた。価格・財務計算は変更していない。

## 未実施

main反映、本番DB/R2のPUT・pointer切替、workflow dispatch、新しいprovider取得、既存rawの移動/削除、lifecycle/retention・認証・課金変更は未実施。rawの新単一prefixへの削除設定はユーザー担当で、本番切替前の確認事項。旧graphのコピーは比較・受入・通常復旧に必要な一時保全だけで、永久archive/pinや旧releaseの永久復元保証は追加しない。
