---
name: improvement-loop
description: screening ranking、E[r]、FV、macro読み、validation等の基盤方法を、事前登録した評価と運用テストで改善するときに使う。個別銘柄の通常判断には使わない。
---

# Improvement Loop

## 正本

[`docs/operations/improvement-loop.md`](../../../docs/operations/improvement-loop.md)と[`docs/reference/estimate-calibration.md`](../../../docs/reference/estimate-calibration.md)を読む。個別銘柄のproposalは`decision-cycle`へ戻す。

## 1改善の順序

1. 現状計測を固定する。
2. 改善仮説をIssue化する。
3. 採否基準を計測前に登録する。
4. design/confirmの両期間で評価する。
5. 採用する最小変更を実装する。
6. public operationを使った運用テストを行う。
7. dated reportへ結果を固定する。
8. PR、review、merge後の継続監視へ進む。

## 規律

短期成績や単一例で閾値を最適化しない。grid search、統計的優位性、backtest実績を主張しない。仮説、採否基準、design/confirm、coverage欠損を後から書き換えない。schema値やreport historyをskillへ複写しない。
