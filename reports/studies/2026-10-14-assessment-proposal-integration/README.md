---
title: "Stock Assessment / Investment Proposal 結合検証"
summary: "#1533のschema24・一回移行・公開consumerと親レビュー修正を含む最終local gate。"
doc_type: report
status: historical
---

# 結合検証

2026-10-14、[詳細設計 #1533](https://github.com/koumatsumoto/baibai-loop/issues/1533)の実装。
作業branchは `codex/1533-assessment-proposal`、draftは
[PR #1536](https://github.com/koumatsumoto/baibai-loop/pull/1536)。
A/Bと自作Owner MCP撤去を含むintegration `037d9e17` と、財務修正後main `878fd7f6` を
通常mergeで取り込んだ。コードの検証headは `8fcc4b0e`。この記録追加後は文書gateを再確認する。
初期の未結合段階の結果は[独立core記録](../2026-10-14-assessment-proposal-core/README.md)へ残す。

## 実装とレビュー修正

評価・独立Reviewをimmutable pairとして原子的に保存し、行動・保存入力・条件・scopeを
Investment Proposalへ固定した。現在previewは保存条件/hashを変更せず別viewとして返す。
Operation/workspace/CLI、共有read_api、Web/Edge、算術・watch・文書を新契約へ統一し、
旧Thesis/CAA/PositionReview runtime・公開経路を撤去した。自作MCP専用は親統合で撤去済みで、
TradingView・Web認証・共有lakegatewayは維持した。

親レビューによる修正は以下を含む。

- latest時刻はPythonのaware datetimeでマイクロ秒まで順序を保持する。refreshは旧判断・実公開時刻と照合する。
- refreshの素材priorと最新supersedesを分ける。旧Aを素材に最新BをsupersedeするCの公開を試験した。
- 負cashでbuy以外を拒否しない。terms未確定buyには希望上限を保存し、現在quoteから推定しない。
- 保存入力はReview以前、quote観測は保存checked_at以前を要求する。公開時の再読取checked_atはfingerprintから除外する。
- 仮説維持holdでも非nullの見返り主張を現在算術で検証する。RR=Noneの旧valuationによる仮説維持holdは保持する。
- 当初理由はAssessmentとProposalの両公開が初回buy以前の場合だけ参照する。後から作った評価・提案は当初理由にしない。
- direct-factは初回UUIDを固定し、既知Execution/Orderを採番前に解決する。同Orderの新部分約定をno-opにしない。
- 公開Edgeのpreviewは最終データ生成時点の結果を再読込することをUIで明示した。動的計算機構は追加していない。

broken/primary verifiedのeconomic sellは現在quote無しでも判断を保持する既存pure契約を維持した。
その経路のremaining_rewardは売却優先判断には使わない。今回の追加gate修正はHoldの見返り主張を対象にした。

## 一回移行

[当時のdomain_v1](https://github.com/koumatsumoto/baibai-loop/blob/5ac69d04c1fb99cbdc80e3f43673e18068971307/tools/cutover/domain_v1.py)は本番application22から最終24へ一度で移す。
中間開発23の自動互換移行は行わない。全台帳事実・非対象tableはpayloadとreplayを保持し、
必要な旧買付参照は最小bindingにする。旧価格・数量・期限をFillから捏造しない。

選択した旧3/4評価・旧提案・保有理由・選択分析根拠とactive workspaceは素材へ退避する。
旧3は当時のhash preimageを検証し、新契約へ承認せず限界を明示する。
旧runのSQL/raw/関連ReviewSet・Triageのidentityとrules参照を照合する。
外部の旧rules内容を独立検算する入力は工具に無く、その保証は主張しない。

新Assessment/Review/Proposal/Operationは空、新run cacheも再生成待ちで開始する。
新成果は切替後に通常Screening/Researchで作る。source/backupは削除せず、
材料コピー後のfresh read transactionでsourceとrunを再読し、writer競合では新outputだけをrollbackする。
合成CLI `--check` / copy rehearsal、原本byte不変、台帳全件/replay一致、参照/hash閉包、workspace保全を確認した。

## 最終検証

全て合成fixture・隔離storeを使用した。個人台帳・原API全文・秘密は公開差分に含めていない。

| 検証 | 結果 |
| --- | --- |
| Python全suite | 4619 passed / 8 skipped、coverage 87.37% |
| format / Ruff / mypy | 成功 |
| import-linter | 34契約成功 |
| 全13 drift | 成功 |
| Bandit / Python依存・build依存監査 | 成功 |
| frontend | 311 passed、lint/build/npm監査成功 |
| Edge | 106 passed、types/typecheck/npm監査/dry-run bundle成功 |
| 移行・schema・appdbの対象検証 | 69 passed、CLI合成rehearsal成功 |

frontend lintは0 error・既存7 warningと新1 warning。新warningは評価ID切替時の表示state同期である。
8 skipは専用R2受入credentialが未設定のため。R2への実書込・削除受入成功を主張しない。

[合成付録](../../../tests/fixtures/domain-assessment-proposal-synthetic.json)をtyped parseして確認した。

- Assessment core: `ce2ec16dda47852c005a079d970cc8e6bcf8c55d05057e1cdd1453b17825502b`
- Proposal draft: `77c287a8cbd18286ccf2c2f80fce44ed6b12e96dadf5d3fc4821e89deae4d100`
- 正式selection: `d545904a1e3c64d4b8d4ef4a0fd040ee757c67a7b8e5fbff8302ecc394ede88d`

各Review hash、Assessment ref、selection scopeが一致し、Assessmentはready/errorsなし。
付録buyの数量300株・notional300000円の算術も一致した。
実A Operation→評価→提案公開→buy binding→完了→previewは別のサービス試験で確認した。

財務mainのrevision v27取り込み後、較正panelのgolden差はrules_hashだけで全数値が一致した。
Candidate Discoveryのmembership/rank/gapは保持し、production/panel/cache identityと
valuation revisionの4 metadataだけを更新した。候補・較正の対象回帰119件と16 subtestsが成功した。

途中gateは旧consumer撤去中のimport/文書/CLI登録、財務revision前のgolden identityで失敗した。
該当差分を修正し、最終全gateを再実行して上表の成功を得た。失敗段階を最終成功と混同しない。

## 未実施

`baibai-batch validate-market-context-stores`はread入口を実行したが、この環境に
`stores/market_context/indicators.sqlite`が無く開始できなかった。
実データ移行・本番store検証・ドメイン本番切替・実broker操作・ブラウザ実機確認は未実施。
main直接push/mergeやintegrationの独断mergeは行っていない。既存の財務ルール本番切替は再実行していない。

## 親の追加移行レビュー：mixed Triage

source22のTriageはnew対象だけをadmitし、holdingは台帳から選ばれる。
`897af0c8`でTriage枝の被覆確認・素材抽出をnew対象だけに限定した。
new=1234、holding=2331、Triage entryにholdingが無い正当な混在入力のcheck/copyが成功し、
holdingのworkspace draftと元payloadを保持する。new entry欠落は引き続き拒否する。
explicit_runは退役前の全targets被覆契約を確認し、holding分析欠落の拒否を維持した。

合成Triage4 fixtureは退役前ed7b2bb7の実契約でparse/admissionを独立確認した。
移行対象試験54件成功。追加修正後の完全local gateはPython4622 passed / 8 skipped、
coverage87.37%、frontend311件、Edge106件と全静的・生成・依存監査・buildが成功した。
merge・本番切替は実施せず、全追跡ファイルの別担当点検と親レビューを待つ。
