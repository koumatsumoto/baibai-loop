"""長期見積り較正 (estimate calibration) harness.

過去 asof の point-in-time リプレイで「見積り・ランキングの長期予測力
(3m/6m/1y/3y/5y horizon)」を計測する L2 の決定論的機械処理。目的は見積り精度の
較正であり、短期 horizon の screen 成績最適化・track record の提示ではない
(docs/doctrine.md 柱 5 / §8 の計測経路)。
"""
