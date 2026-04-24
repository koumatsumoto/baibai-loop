---
run_date: "YYYY-MM-DD"
universe_size: 整数
filters:
  min_market_cap_oku: 300
  min_avg_turnover_oku: 2
  exclude_listed_under_months: 6
tickers:
  - ticker: "XXXX"
    name: "..."
    per_forward: 8.2       # null if 会社予想 EPS 未公表
    per_trailing: 9.5
    pbr: 0.72
    ev_ebitda: 4.8
    p_s: 0.6
    pcfr: 5.1
    sector_33: "業種名"
    threshold_hit:
      - sector_median_under_20pct_and_self_range_bottom_20pct
      - price_down_60d_and_valuation_sigma_down
      - sector_rotation_short_sell
---

# Screened: YYYY-MM-DD

**成分**: 4 成分アーキテクチャの **(b) スクリーニング通過銘柄**（[`/docs/components/screened.md`](/docs/components/screened.md)）

**レイヤー**: 事実レイヤー（解釈は入れない）

**閾値条件**: 以下 3 種の OR 条件、最低 1 つ満たす（[`/docs/screening/mechanical-v1.md`](/docs/screening/mechanical-v1.md)）

- 条件 A: 業種中央値比 -20% 以上 かつ 過去 3 年自己レンジ下位 20%
- 条件 B: 過去 60 営業日 -15% 以上下落 かつ valuation 1σ 以上下方（業績悪化なし）
- 条件 C: セクター RS 下位 20% + 個別が業種平均下回り（業績悪化なし）

## 1. 実行概要

- 実行日: YYYY-MM-DD
- Universe サイズ: XXX 銘柄
- 通過銘柄数: XX 銘柄

## 2. 通過銘柄の事実メモ

特筆すべき事実（複数閾値 hit 銘柄、同業種集中、異常値の除外など）を事実として記録。解釈は入れない。

- [事実 1]
- [事実 2]

## 3. 実行環境

- データソース: J-Quants core（日足・財務サマリー・業績予想）+ EDINET + JPX
- 取得失敗の有無: [有の場合は対象銘柄と理由を列挙]

---

研究選定は [`/docs/components/research.md`](/docs/components/research.md) の選定プロセスに従う。通過銘柄のうち `view/` で tailwind / neutral の業種/地域のもののみが research 候補となる。
