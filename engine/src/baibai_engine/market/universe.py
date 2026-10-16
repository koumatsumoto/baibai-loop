"""Eligible TSE market segments; native product classification is separate."""

# The scope is the main domestic markets: everything a private investor can buy on
# ordinary terms, less TOKYO PRO MARKET and the residual segments. The exchange
# renamed its segments in April 2022, so a point-in-time master from before then
# carries the older names for the same markets. Both vocabularies are listed
# because the scope is about which markets, not about what they are called; a
# current master never carries the older names, so historical replay is the only
# place they appear.
ELIGIBLE_MARKETS = {
    "PRIME",
    "STANDARD",
    "GROWTH",
    "プライム",
    "スタンダード",
    "グロース",
    "東証一部",
    "東証二部",
    "マザーズ",
    "JASDAQ スタンダード",
    "JASDAQ グロース",
}
