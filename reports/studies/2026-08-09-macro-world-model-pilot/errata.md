# Macro World Model pilot errata — 2026-08-09

この errata は 2026-08-07 cycle の frozen workspace と発行済み v4 を変更せず、独立監査で確定した所見を dated evidence として記録する。builder と authoring contract は fix-forward し、blind freeze の検証可能性を保つ。

## Relation classification と hard fail

`world-model.yaml` の次の 2 edge は `relation_kind` を誤用している。

- `e-real-rate-portfolio`: 実質割引率から portfolio cashflows への DCF / valuation 関係を `accounting_identity`、`claim_strength: identified` としている。これは会計恒等ではなく `model_based_relation` に相当する。
- `e-demand-portfolio`: 家計需要から portfolio cashflows への行動的伝達を `accounting_identity`、`claim_strength: supported` としている。これは empirical または judgmental な関係に相当する。

自己 hard-fail check は 0 件と記録したが、独立監査はこの誤分類を preregistration hard fail #4 相当 1 件と判定する。自己申告と独立監査の差は、relation label の意味を外部監査する必要性を示す。graph node `portfolio-cashflows` も economy-level の命名ではない。

## 円・JGB の機械監視と座標

発行済み v4 の scorecard と monitoring `machine_conditions` には `usd_jpy` と `jp.10y` が 1 件もない。円と JGB / 割引率は standing structural exposure だが、日次 trigger がこの面を機械監視できない。

connection hint は「円157.8」と記載し、これは 2026-08-06 の観測値 157.8323 に対応する。現 store で確認できる 2026-08-07 の実観測は 158.3355 であり、report `as_of` と hint の座標に 1 営業日のずれがある。

## v4 scenario projection

world model の adverse scenario `energy-trade-squeeze` は energy / trade shock が実質所得と growth を圧迫する stagflation tail を持つ。v4 projection はその favorable inverse を bull に割り当てたため、発行済み v4 trio には adverse 側の stagflation tail が含まれない。projection note は inverse mapping を記録するが、この情報損失を明示していない。

## Evidence snapshot revision history

cycle の `evidence-snapshot.json` は revision 窓導入前の形式で 138,681 行ある。全 L1 vintage history を `revisions` に載せたことが主因であり、反復 cycle の canonical storage boundary を満たさない。

この snapshot は frozen workspace の一部であり、発行済み v4 の導出元と freeze hash の整合を保つため再生成しない。builder は観測 24 か月、前回 head `as_of` 以降の revision arrival、series ごとの件数 cap を適用する形式へ fix-forward する。

## SLOOS 最新性確認

2026 年 7 月調査の SLOOS は Federal Reserve Board の公表 calendar どおり **2026-08-03 14:00 ET** に公表され、2026-08-07 より前に利用可能だった。Federal Reserve の release page も July 2026 survey と 2026-08-03 の更新日を示す。

- Federal Reserve Board, [Calendar: August 2026](https://www.federalreserve.gov/newsevents/2026-august.htm)
- Federal Reserve Board, [July 2026 Senior Loan Officer Opinion Survey on Bank Lending Practices](https://www.federalreserve.gov/data/sloos/sloos-202607.htm)

したがって、evidence pack が April 2026 版を最新公表として引用したことは self-check (f) の miss である。July 版は C&I standards が概ね不変、大・中堅企業の C&I demand が強化、CRE standards が概ね緩和と報告しており、April 版だけでは 2026-Q2 の credit state を反映できない。
