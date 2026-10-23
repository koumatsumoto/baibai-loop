---
title: "四領域のAssessment・Review・Proposal結合検証"
summary: "#1567の現行Assessment参照案、CIの保存契約回帰、固定3銘柄の限定的な意味検証と隔離保存読戻し。"
doc_type: report
status: active
---

# 四領域の結合検証

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

合成回帰は [PR #1568](https://github.com/koumatsumoto/baibai-loop/pull/1568) の固定HEAD
`f83b6a0d3d86b50b5d08fc8fc65cefa07cdb8f51` の
[GitHub CI](https://github.com/koumatsumoto/baibai-loop/actions/runs/39817134898)で成功した。
全suite 6083 passed / 8 skipped、coverage 90.00%。ローカルtestsuiteは実行していない。

合成の独立役identity、primary確認、手書き再計算goldenは保存契約の試験入力であり、
別の人間/AIが原典・経済的因果・反証を独立Reviewした証明ではない。
文章の件数や特定語句をvalidatorで必須化せず、判断品質・予測力・利益改善をこの回帰で主張しない。
新しい重要根拠を評価本文へ記録しなかったことまではhashで検出できない。

## 固定3銘柄の実入力検証（2026-10-23）

実入力の意味を作者が検討し、別の検証担当が原稿と根拠・算術をレビューした。
合成の手書きgoldenとは別の確認だが、独立した投資専門家の保証や予測精度の実証ではない。

- code: `48b1d3d8cc002781973524dbdedab29a9daec106`
- release: `20261022T163903Z-4d0a9394-ea4dc4078732`
- run: `run-revision-b917cfedc7cd498c991e980d882429fc`、価格cutoffは2026-10-21
- Research日: 2026-10-23、Selection input_known_at: `2026-10-23T12:15:40.663086+09:00`
- Operation: `research-op-630d42519b1b4033acbca4ea5286e5c4`
- Selection SHA256: `b1446e30f0a42d7645ec73be593d31e3f3a65ff85928f3f01d6ec77088b2e2bd`

固定releaseから正規hydrateし、隔離application DBでprepare・評価/Review保存・Proposal公開・exact読戻しを行った。
このSelectionは保存経路の固定ケースであり、現在のマクロ環境から新しく選んだ推奨候補ではない。
後着の企業別リサーチ質問は作者ノートへ分離し、保存済みSelectionへ挿入しなかった。
10月22日受領の外部評価や10月23日再計算を、10月21日に利用可能だった情報と偽らない。

| ケース | 統合で変わった判断 | 保存した行動 |
| --- | --- | --- |
| NCD 4783 | 低PERと現金の多さだけで価値を加算しない。会社の資金方針、顧客集中、労務費、既知ESOP、配当権利と終価を条件付きシナリオへ接続。外部未カバーは悪評価ではなく、RSI/BBも価値の裏付けではない | 条件付き比較後の `no_action`。割安仮説は uncertain のまま |
| トヨタ 7203 | 営業利益と純利益の方向が異なる。会社/外部の売上予想はほぼ同じでも純利益は約15.3%異なり、金融損益・税・株数basisの確認課題となる。外部強気は独立の買い票ではない | 正常利益・金融事業の橋渡し未完了による、評価参照付き `defer` |
| MUFG 8306 | 金利上昇効果は会社計画の前提へ既に含まれる。追加加算せず、預金調達費・信用費用・OCI・規制資本を対照。generic cash-minus-debtを余剰株主資産としない | 利益・規制資本・分配の橋渡し未完了による、評価参照付き `defer` |

NCDのBaseは36か月、作者の要求率10%、売上340億円・営業利益32億円、終期PER10倍等の条件付き仮定。
株価2483円に対する年率約6.45%、条件付きPmax約2250円となるが、会社予想や唯一の適正価格ではない。
PER8〜12倍で年率約−0.20〜12.36%へ変わり、採否は反転し得る。強い確信や指値買いへ読み替えない。
配当方針・将来cash conversion・開始cashの仮定を原稿で分離し、cash不足感度と配当権利の二重計上を確認した。

3件とも一次資料確認は `partially_verified`。関連箇所を実読した範囲であり、有報全頁の精読、
価格履歴からの全指標独立再構築、将来の持続利益の確定は主張しない。

## 保存結果と境界

- Proposal: `investment-proposal-4fe4572d-7eee-40ef-a997-1a073a16cee9`
- draft SHA256: `a06265bd12ceb7b7ef0f65a7c4afbe4bfbfd2f87dc14e282ead854a29950e91a`
- Review: `proposal-review-b82a65d9-8912-4796-8f0b-40dc857cba25`
- 隔離保存日時: `2026-10-23T13:21:02.864954+09:00`

正規checkは `valid=true` / `review_binding=match` / blockersなし。
読戻しは作者原稿と比較し、serviceの公開日時と同一時刻のUTC表記正規化を除き、本文・inputs・scope・Review・exact評価参照が一致した。
Proposal発行前後のAssessment/Review・ledgerは不変。予約・注文・約定は0。

Proposal contextが未import台帳を受け付けないため、隔離DBだけに明示的な開発用合成opening balance 1円を入れた。
本人資産ではなく、経済的見送り理由でもない。このDBやProposalを本番へコピーしない。
実口座の提案、マクロ起点の新しい候補選定、未来の利益改善、未解決2社の価値評価完成は、本検証の完了に含めない。

判断理由と一次資料の照合履歴は [Issue #1567](https://github.com/koumatsumoto/baibai-loop/issues/1567) に残す。
較正の欠損と過去入力の適用限界は [#1569](https://github.com/koumatsumoto/baibai-loop/issues/1569) で別に判定する。
新schema・保存層・自動score・必須コメント欄は追加しない。既存の全文参照と明示的な不足記録で有限ケースを追跡できたため、
SelectionをProposalへ再投影する追加設計は引き続き保留する。

一次資料の主要参照：
[NCD 決算短信（2026-08-07）](https://www2.jpx.co.jp/disc/47830/140120260805510616.pdf)、
[NCD 中期計画（2026-05-15）](https://www.ncd.co.jp/ir/management/plan/pdf/vision2029.pdf)、
NCD保存有報 `S100YEMH`（2026-06-19、type1原本SHA `45b0c26da66b2a7ea5359c60b3de2ef2db631010f332b0d8a0ea77de7b08dd8b`）、
[トヨタ FY2027 Q1 summary](https://global.toyota/pages/global_toyota/ir/financial-results/2027_1q_summary_en.pdf)、
[トヨタ説明資料](https://global.toyota/pages/global_toyota/ir/financial-results/2027_1q_presentation_2_en.pdf)、
[MUFG Q1ハイライト](https://www.mufg.jp/dam/ir/fs/2026/pdf/highlights2606_ja.pdf)、
[MUFG経営戦略](https://www.mufg.jp/profile/strategy/index.html?link_id=gnavi_profile_internal_strategy)。
動的ページの現在内容を過去snapshotの代用にはしない。参照範囲と時点は各Assessmentに記録した。

隔離Operationは `2026-10-23T13:26:59.817353+09:00` に正規completeし、終端 `completed` と作者指定payloadの完全一致を確認した。
この終了は調査手順の終了であり、人間の採否・本番反映・注文の意味を付与しない。
