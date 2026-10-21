---
title: "Stock Assessment / Investment Proposal 独立coreの実装記録"
summary: "#1533 の独立coreと結合待ちを区別した、合成fixtureによる検証記録。"
doc_type: report
status: historical
---

# 独立coreの実装記録

2026-10-14、[詳細設計 #1533](https://github.com/koumatsumoto/baibai-loop/issues/1533)の最初の実装単位。
base は `codex/1480-domain-integration` の `ed7b2bb7`、作業branchは
`codex/1533-assessment-proposal`。本番storeと現行公開consumerの切替は未実施。

[Stock Assessment](../../../engine/src/baibai_engine/research/assessment.py)は、事業理解・所見・
価値・反証を保存する。現金と現在の購入適格性を評価結論へ混ぜない。
[独立Reviewとのpair保存](../../../engine/src/baibai_engine/research/assessment_store.py)は
同じtransactionで発行し、同IDの内容変更を拒否する。最新評価が未確定またはunsupportedでも
古いsupportedへfallbackしない。参照は評価ID・core hash・Review IDを照合する。

[差分refresh](../../../engine/src/baibai_engine/research/assessment_refresh.py)は旧source/factの
値・日付を保持する。今回の期間・今後分配・確認理由は未入力とし、旧Reviewを付け替えない。
`refresh.prior_assessment_ref`は素材にした旧pair、`supersedes_id`は最新pairを指す。
この二つを等しくする追加gateは置かない。

[Investment Proposal](../../../engine/src/baibai_engine/research/proposal.py)は行動別の型と、
当時の入力・対象範囲・具体条件を持つ。現在previewは後続の別読取として結合する。
[入力fingerprint](../../../engine/src/baibai_engine/research/proposal_fingerprint.py)は
収集時刻の`checked_at`だけを除外し、実際の価格・台帳世代・policy等の事実を比較する。
Decimalの同値とUTC/JSTの同一instantを正規化する。

[条件付きbuy](../../../engine/src/baibai_engine/research/entry_policy.py)は実quoteと勧めるlimitを
分けて計算する。現在価格がpmax超でも、他gateが満たされlimitがpmax以下なら指値を試算できる。
[保有判断](../../../engine/src/baibai_engine/research/holding_policy.py)は今回の仮説維持確認と
当日起点の残存見返りを区別する。[呼値](../../../engine/src/baibai_engine/position/price_grid.py)は
公式その他tableの保守的共通gridと既存普通株分類を使う。提案価格・数量が揃うことから、
注文受理や約定を推定しない。

[manual binding](../../../engine/src/baibai_engine/position/decision_references.py)は
既存ledger CASに原子的に追加する。同参照を使う追加買付event全件を照合し、bindingだけを
先にcommitしない。broker draft入口のdirect-fact署名は親の確認待ち。

## この段階の検証

全て合成データ。公開fixtureへ個人台帳・原API全文・秘密は含めていない。
[合成payload](../../../tests/fixtures/domain-assessment-proposal-synthetic.json)のAssessmentは
typed parse後のcanonical hashと独立Reviewが一致した。
Proposalの`selection_sha256`は説明値であり、実Operationへの参照整合成功を主張しない。

- Assessment / refresh：87件成功。旧根拠の日時保持、新期間・分配の独立検算、未知、
  原子的保存、latest選択、exact参照、厳密なversion整数を確認。
- ledger binding / 既存broker draft：30件成功。同時commit、途中失敗rollback、競合・stale、
  UUIDを保持する保存draft、厳密versionと旧draftの再prepare案内を確認。
- 呼値 / 既存購入判断：61件成功。公式全価格帯の境界、期限、分類、旧購入policyを確認。
- 新Proposal / buy / holdingの試験は正式Aの`research.selection.ResearchTarget`未到着で
  collectionが停止。typed Proposal hashとservice整合の成功は未主張。

完全local gateを実行し、format・Ruff・全13 drift・Bandit・Python依存/ビルド依存監査と、
frontend 299件・edge 93件、各build/type/lint/監査、Worker dry-run bundleが成功した。
mypyはA型未到着の3エラー、import-linterは既存owner参照の結合調整待ち、Python全suiteは
A型未到着のcollection errorで停止した。完全gate成功とは扱わない。

A依存の4ファイルを明示除外した追加回帰は4447件成功・8件skip、coverage 85.53%。
除外は`test_conditional_buy_terms.py`、`test_holding_proposal.py`、
`test_investment_proposal.py`、`test_proposal_fingerprint.py`。
skipは専用R2受入credential未設定であり、本番受入成功とは扱わない。
途中の1失敗は環境に残った旧packageの生成bytecodeによるnamespace検出だった。
tracked sourceが無いことを確認し、該当`__pycache__`だけ整理した後に再実行した。

## 結合待ち

A/Bの結合順を親が調整中。共通schema、Operation/workspace、CLI、Web/MCP、最終一回移行
entrypointは保留指示に従い変更していない。旧契約のconsumer撤去は最終統合で揃える。
新規buyのcommon factorsを取得するwarning関数の追加入力、direct-factの初回参照署名を
親へ具体的に返した。新Proposal/新Operationを移行工具で自動承認する経路は作っていない。
