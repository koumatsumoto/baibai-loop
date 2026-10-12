# Baibai Loop

一人で日本株の割安さと企業価値の変化を評価する意思決定基盤です。Valuation（独自価値評価）、Market Context（市場環境）、External Assessment（外部参考評価）の三分析領域で整理を進めています。機械処理が観測と候補集合を作り、AIが調査・判断を提案し、人間が最終裁定とbroker操作を行います。三領域の既存計算・保存済み読取を整理し、明示開始の有限調査・部分完了・提案を既存Operationへ接続しています。配置整理と通常運用への統合受入は継続中です。現行配置と未移行範囲は[architecture](./docs/architecture.md#repository-map)を参照します。

初めて読むときも、調査・運用・開発の入口を探すときも、[docs portal](./docs/README.md)から始めます。保存済みReview SetのAI判断はResearch Triageが所有し、明示draftの入口は`baibai-engine research triage`です。

財務・倍率の計算はValuation、価格文脈・信用需給はMarket Context、候補探索と分析結果の組立はScreeningが所有します。境界と残配置は[architecture](./docs/architecture.md)を参照します。
