---
playbook_id: fcf-yield-discount
signal_lane: fcf-yield-discount
status: active
---

# FCF Yield Discount

## Purpose

EDINET CSV-derived metrics の operating cash flow と capex を使い、営業 CF ではなく FCF 利回りで安い候補を扱う。

この playbook の狙いは、会計利益や PBR では割安に見えにくいが、設備投資後の現金創出力に対して時価総額が安い銘柄を拾うこと。OCF yield だけでは設備投資負担を過小評価するため、FCF を primary thesis にする。

## Entry Focus

- `fcf_yield` が rule threshold を満たす。
- FCF がプラスで、capex source / quality が確認できている。
- CFO が大きく悪化していない。
- 金融、電気・ガスなど CF の意味が通常事業会社と異なる業種ではない。

## What Must Be True

- CFO が売掛金回収、在庫圧縮、前受金増加など一過性の運転資本改善だけで膨らんでいない。
- capex 抽出が維持投資・成長投資の実態と大きくずれていない。
- FCF の高さが、必要投資の先送りや事業縮小の結果ではない。
- 利益・売上・受注・解約率など、現金創出力を支える事業指標が急悪化していない。

## Common Traps

- 直近期だけ capex が落ち、FCF yield が一時的に高く見える。
- 運転資本の巻き戻しで翌期 CFO が急減する。
- 研究開発やソフトウェア投資が expense / capex のどちらに出るかで同業比較が歪む。
- 大口案件、前受金、補助金、税金還付など一過性 cash-in を通常 FCF と誤認する。

## Required Research Checks

- FCF snapshot: EDINET CFO / capex / FCF / market cap / fcf_yield を candidates と一次資料で突合する。J-Quants `ocf_ttm` と混ぜて再計算しない。
- EDINET source trace: `edinet_source_doc_id`、`edinet_document_type`、`edinet_source_submit_datetime`、`edinet_source_period_start`、`edinet_source_period_end` を確認し、対象期間が FCF thesis と一致するか確認する。半期報告書 / 訂正半期報告書では `edinet_source_period_end` が fiscal year end を指すことがあるため、CF 計算書の実際の測定期間を一次資料で確認する。
- Capex quality: 維持投資、成長投資、投資サイクル、翌期投資計画を確認する。
- Working capital quality: 売掛金、棚卸資産、前受金、仕入債務、税金影響を確認する。
- Earnings quality: 利益率、特損益、減価償却、受注・解約など現金創出力の裏付けを見る。
- Shareholder return check: 配当政策、自社株買い、DOE or 配当性向、減配リスクを確認する。
- Entry / Exit / Invalidation: FCF thesis が崩れる条件と正常化 target を明示する。
- Position size: single evidence path / multiple independent evidence paths と流動性を確認する。
