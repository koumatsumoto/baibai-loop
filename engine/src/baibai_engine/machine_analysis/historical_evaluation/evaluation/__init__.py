"""較正の評価指標: rank IC・decile・候補replay・trap・条件付きspread・収束実現。

統計の誠実性 (docs/doctrine.md の計測経路):
- cohort (月次 asof) は forward 窓が重複し独立でないため、有意性検定は行わず
  「効果量 (median/mean excess) と cohort 勝率」で報告する。
- 超過リターンの一次基準はcommon eligible母集団の中央値。構成差や交絡を
  除去した選定効果の因果推定ではない。
  benchmark ETF は市況文脈の参考値。
- 個別銘柄の年率換算は E[r] との較正比較に使う。売買・資金配分を再現した
  ポートフォリオの累積収益・年率収益・Sharpe 等の運用実績は主張しない。"""
