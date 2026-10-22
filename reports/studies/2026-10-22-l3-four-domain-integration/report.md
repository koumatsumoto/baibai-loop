---
title: "四領域のAssessment・Review・Proposal合成結合検証"
summary: "#1567の現行Assessment参照案と、判断理由・固定入力・旧Review拒否の合成回帰。実銘柄受入は未完了。"
doc_type: report
status: active
---

# 四領域の合成結合検証

[Issue #1567](https://github.com/koumatsumoto/baibai-loop/issues/1567)のうち、保存済み四領域から
Assessment・Review・Proposalまでの接続を検証する。初期L2と執筆案内は#1530側であり、
本検証の完了を初期実装の完了条件へ追加しない。

## 採用した接続と保留した案

現行のReviewed Assessment全文参照を維持する。Proposal contextには評価本文とReview全文が入り、
保存Selection digestとexact評価ID/core hash/Review IDを使って追跡できる。
重要な支持・相反・不足は既存のFindings・投資理由・最強反証へ記し、行動理由へ接続する。
新しいmodel、score、必須コメント、provider、保存層、schema、購入・売却gateは追加しない。

保存Selectionの`project_research_inputs`をProposal contextへ直接追加する案は保留した。
元の窓・basisを同時表示する利便性はあるが、それ自体は重要所見の採用・独立Reviewを保証せず、
Proposalで未評価のL2観測を新たな判断根拠にする経路も残る。保存入力の再参照に起因する
実際の取り違えが確認された場合に、読取用payloadだけの最小変更を再検討する。

## 合成ケースと保存経路

[`test_research_l3_integration.py`](../../../tests/engine/test_research_l3_integration.py)は
既存のrun・価格・外部観測・Market Context writerと隔離application DBを使う。
実prepareから固定入力を保存し、手書きAssessment/Reviewをpromoteして、Proposalを公開・読戻す。
Research入力collector・Technical・外部評価readerの正常経路はstubへ置換しない。
ネットワークを拒否し、個人台帳・未公開Research・provider取得は使わない。

| ケース | 評価・相反・不足 | 行動 |
| --- | --- | --- |
| wait | 継続価値1100円、12か月・要求率10%。金利逆風を固定負債とcashで対照。価格はBB下側・RSI0 | 実quote1100円を保持し、Pmax1000円の指値待ち |
| current | 同じ価値支持と弱い価格局面、実quote900円 | `at_current_limit`900円、300株、27万円 |
| broken | BB上側・RSI100、外部は強気でも、一次資料で恒常収益源の喪失を確認 | broken/unsupportedからno_action |
| gaps | 60barでSMA200不足、SMA20等は利用可能。外部は明示not_covered、目標null | 非重要な不足を残して指値待ち。他領域を停止しない |
| unknown | 借換条件が重要な不足で価値未確定 | uncertain/unresolved、数値検算なし、評価参照付きdefer |

価格は一定値249観測と最後の変化から構成し、短い履歴だけ60観測とする。
移動平均・BB・RSIとMarket Contextの騰落率は同じ価格情報で、独立票にしない。
架空の二媒体のifis評価も一原評価として扱い、媒体数を支持票へ変換しない。
Sourceの既存locator/used_forとSelection digestに、保存field・計算revision・basis・実効履歴・seed、
外部対象期・株式basis・受領時刻を残す。

保存後に価格と外部目標を変更し、元runを除いても、再開投影・評価pair・Proposalが不変であることを
確認する回帰を追加した。再開時のcollectorとTechnical再計算は明示的に拒否し、現在previewのquoteは
別fieldで変化することを確認する。旧snapshotのTechnical欠落と旧BB欄の保持は、初期実装で追加済みの
`test_bollinger_context_is_frozen_in_research_inputs`を再利用する。

negative caseは採用sourceのbasis説明、仮説理由、最強反証を変更して元Assessment Reviewでのpromoteを、
行動理由・比較理由・再確認条件を変更して元content ReviewでのProposal公開を拒否する。
元Reviewを保存した後は`update_review_hash`や`bind_review`を呼ばず、拒否条件を消さない。
拒否後に評価pairやProposal/index/buy bindingが保存されていないことも確認する。

## 検証状態と限界

本変更のローカルfocused test・suiteは実行しない。対象commitのGitHub CIで検証し、
その結果を確認するまでは成功扱いにしない。

合成の独立役identity、primary確認、手書き再計算goldenは保存契約の試験入力であり、
別の人間/AIが原典・経済的因果・反証を独立Reviewした証明ではない。
文章の件数や特定語句をvalidatorで必須化せず、判断品質・予測力・利益改善をこの回帰で主張しない。
新しい重要根拠を評価本文へ記録しなかったことまではhashで検出できない。

実銘柄受入は未実施であり、本Issue全体を完了としない。rootが受入済みの互換release ID・manifest SHA、
code SHA・run・Research日・known_atを固定してから、事前固定の7203・8306・4783を公開資料だけで
検証する。新しいみんかぶ観測等の不足を別releaseから補完せず、個人保有は読まない。
固定releaseが後日保存世代である場合は厳密な当時点vintageの再現と区別し、少数例を将来利益の実証にしない。
