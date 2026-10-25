"""銘柄評価・独立Reviewと、価格・資本へ適用するTrade Proposalを組み立てる。

評価の根拠はimmutable pairとして保存し、行動の条件と現在previewを分ける。
確認済みの取引事実とportfolioのreplayはpositionが所有する。
"""
