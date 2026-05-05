---
playbook_id: strict-net-cash-discount
signal_lane: strict-net-cash-discount
status: active
---

# Strict Net-Cash Discount

## Purpose

EDINET CSV-derived metrics の cash と interest-bearing debt を使い、厳密な net cash に対して時価総額が安い候補を扱う。J-Quants proxy の cash-rich より優先する。

この playbook の狙いは、PER / PBR だけでは見えにくい「財務余力そのものに対して安い」銘柄を拾うこと。小型・中型で市場カバレッジが薄く、事業成長よりも balance sheet の margin of safety が thesis の中心になる銘柄を想定する。

## Entry Focus

- `net_cash_to_market_cap`、`price_to_equity`、`equity_ratio` が screening rule を満たす。
- EDINET 由来の debt / cash が取得できている。
- 営業赤字ではない。
- 金融、電気・ガスなど balance sheet の意味が通常事業会社と異なる業種ではない。

## What Must Be True

- 有利子負債の範囲が screening 抽出と一次情報で大きくずれていない。
- 現金が事業維持、規制資本、拘束性預金、巨額運転資本に実質的に拘束されていない。
- PBR が低い理由が、資産毀損や構造的 ROE 低迷だけで説明されない。
- 株主還元、資本効率改善、政策保有株縮減、事業再編など、balance sheet の価値が市場に伝わる経路がある。

## Common Traps

- 現金は多いが、受注前受金・顧客預り金・短期運転資金として自由に使えない。
- 有利子負債 tag の取り漏れ、リース債務、退職給付、偶発債務を見落とす。
- 安い理由が慢性的な低 ROE / 低成長で、net cash が re-rating catalyst にならない。
- 親子上場、支配株主、流動性不足で資本政策が動きにくい。

## Required Research Checks

- Net cash snapshot: cash / debt / net_cash / market cap / equity を candidates と一次資料で突合する。
- Debt quality: 借入、社債、リース、偶発債務、保証債務、退職給付を確認する。
- Cash usability: 現金の拘束性、運転資本、設備投資予定、M&A 資金、規制資本を確認する。
- Asset discount and capital efficiency: PBR、ROE、政策保有株、資産売却可能性を確認する。
- Shareholder return check: 配当政策、自社株買い、DOE or 配当性向、減配リスクを確認する。
- Entry / Exit / Invalidation: net cash thesis が崩れる条件と re-rating 経路を明示する。
- Position size: single signal / multiple signal と流動性を確認する。
