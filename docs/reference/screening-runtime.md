---
title: "Screening runtime reference"
description: "Security Analysis、4つの価値評価法、Review Set、Research Triageの実行契約"
doc_type: reference
owners: [screening]
last_reviewed: 2026-08-31
---

# Screening runtime

## 役割

screeningは全対象銘柄のobserved / derived / estimateをSecurity Analysisへ固定し、4つの価値評価法から有限のReview Setを作る。Review Setは調査対象を人間が読める量へ絞る機械成果物であり、投資判断ではない。Research Triageだけが各entryへ`research / skip`を発行する。

E[r]、FV、macro context、portfolio stateは参考文脈である。Review Setのnomination、membership、orderには使わない。

## Public CLI

```bash
uv run baibai-engine screening run --asof YYYY-MM-DD [--runs-db PATH]
uv run baibai-engine screening review-set publish --asof YYYY-MM-DD \
  --run-revision-id ID [--runs-db PATH] [--output-path PATH]
uv run baibai-engine screening review-set show --review-set-id ID \
  [--runs-db PATH] [--output-path PATH] [--force]
uv run baibai-engine screening research-triage publish DRAFT.yaml \
  [--db PATH] [--runs-db PATH]
uv run baibai-engine screening prune --keep N [--runs-db PATH]
```

`run`のexit 2はpublication済みpartial warningである。warningを確認してから同じ`run_revision_id`で後続へ進む。Review Setの再表示に再発行を使わない。

## Security Analysis

run store schema v5は次を保持する。

- `screening_run`: run identity、as-of、rules/model identity、universe size
- `security_analysis`: runに属する全銘柄の観測値・導出値・参考見積り
- `review_set`: source runに束縛した再現可能なReview Set

Security Analysisは`observed / derived / estimate`を混同しない。欠損を0へ補完せず、解釈・因果・売買判断を書かない。

## Candidate Discovery v2

共通eligibilityは時価総額、売買代金、上場期間、JPX flag、必須factの有無だけを扱う。その後、各approachが独立に上位20件をnominateする。

| valuation approach | 主座標 | target |
| --- | --- | ---: |
| `current-earnings-power` | sector-relative current PER、current cash-flow yield | 6 |
| `normalized-earnings-power` | 3FY normalized PERのsector gap | 5 |
| `asset-value` | 正のasset-backed ratio、PBR context | 5 |
| `reinvestment-value` | 割安なsector-relative P/S、sector-relative capital-return proxy / margin、growth、FCF yield | 4 |

Asset Valueは`asset_backed_ratio > 0`だけをnative eligibilityとし、銀行業、保険業、その他金融業、証券・商品先物取引業を除外する。金融業ではcash、debt、securitiesが事業上のasset / fundingそのものなので、非金融企業向けgross asset proxyを残余価値として適用しない。`net_cash_to_market_cap`は`analysis.asset_value`へ残すが、eligibilityとorderには使わない。orderはasset-backed ratio降順、PBR sector gap昇順、PBR昇順、equity ratio降順、ticker昇順である。

Reinvestment Valueは正のP/S、sales growth、FCF yield、operating profit、sales、assets、equity ratioと、有限のdebt / cashを要求する。さらに`p_s_sector_gap < 0`、`operating_margin`と`operating_return_on_capital_proxy`がそれぞれ同じsector 33のmedian以上であることを要求する。median populationは、この共通eligible母集団のうち既存のReinvestment入力がすべて成立する行である。sector母数が`MIN_SECTOR_MEDIAN_POPULATION`未満の場合だけ同じpopulationのmarket medianへfallbackし、境界件数はsector medianを使う。通過後のorderはP/S gap昇順、capital return降順、sales growth降順、margin降順、FCF yield降順、ticker昇順である。

primary coordinateがnull、非有限、またはapproachの要件を満たさない銘柄は、そのapproachでnominateしない。金融sector除外、quality threshold、median population / fallback、従キー、tickerまでmethod hashに含め、同じSecurity Analysisとrulesから同じ結果を再構成する。

## Review Set v1

Review Setの上限は20件、representation targetは`6 / 5 / 5 / 4`である。構成は次の決定順を持つ。

1. まだ満たしていないapproachを最も多く同時に覆うticker
2. 全approachからの`support_count`が大きいticker
3. nomination rankを昇順に並べた`rank_vector`
4. ticker昇順
5. target充足後も同じoverlap-first順で20件まで埋める

同じtickerは1entryだけ持つ。各entryは`review_position`、identity、`nominations`、`support_count`、`rank_vector`、grouped `analysis`を持つ。`analysis.expected_return`はestimate snapshotであり、構成権限を持たない。

publisherはsource run、as-of、rules hash、method hash、全Security Analysisからpayloadを再計算し、不一致を拒否する。Review Setは短期run cacheに置き、日次membership履歴を別保存しない。

## Review Basis

Review Setは`review_basis.judged_through_research_triage_id`を持つ。Research Triage publisherはこの値とapplication DBのheadを照合する。古いbasisからの分岐、別runへの付け替え、entryの追加・削除を拒否する。

## Research Triage v1

application DB schema v19の`research_triage`はReview Set全entryをexactly onceで保持する。

- `research`: contiguousな`priority`、`rationale`、`research_question`、`key_risk`が必須
- `skip`: `rationale`が必須で、`priority`、`research_question`、`key_risk`は禁止

scaffold は Review Set の as-of 以下で最新の Macro Context を application DB から選び、`macro_context_id`へ束縛する。Context が無ければ `null` は正常、古ければ warning であり、どちらも Review Set の nomination、membership、orderを変えない。publisherは`review_set_id`、`run_revision_id`、`as_of`、全ticker、Review Basisに加え、Contextの存在と未来参照を検証する。eligible Contextがあるのに`null`は拒否する。明示的に古いeligible revisionを選ぶことはできるが、選択理由を既存の判断文へ残す。`review_position`、non-empty `nominations`、support、E[r] / FV / data-quality文脈を`machine_snapshot`へ焼き込み、発行後payloadはimmutableである。

Research TriageはResearch Setのadmission可能範囲を定める。人間は`research` entryの部分集合だけをResearch Setとして確定でき、`research prepare`はapplication DBから毎回再解決してこの境界を検証する。

## Daily batch

定常日次経路は次だけを自動実行する。

```text
screening run -> review-set publish -> web materialize -> publish
```

Research Triageは人間判断なので自動発行しない。migration、全期間再取得、Research Set確定、Capital Allocation Assessmentもdaily batchへ載せない。

## Pruneと履歴

`prune --keep N`は新しいrunをN世代残し、子のSecurity AnalysisとReview Setを同一transactionで削除して`VACUUM`する。Research Triage、thesis、Capital Allocation Assessment、Position Reviewはapplication DBのcanonical judgmentなのでrun pruneから独立する。

## Market store schema

screeningが読むmarket storeのtableとidentityは次のとおり。列の意味、PIT、coverage、source authorityは[data-sources.md](./data-sources.md)と[valuation-metrics.md](./valuation-metrics.md)を正本とする。

- `jquants_daily_bars(ticker, traded_at, open, high, low, close, volume, turnover_value, adjustment_*)` — 日次価格と出来高
- `jquants_fin_summaries(ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, sales, operating_profit, ordinary_profit, profit, forecast_profit, forecast_ordinary_profit, cfo, cash_eq, total_assets, equity, fiscal_period, fiscal_year_end, period_start, period_end, dps_actual_annual, dps_forecast_annual, treasury_shares, equity_to_asset_ratio, dividend_q1, dividend_interim, dividend_q3, dividend_year_end, dividend_total_annual, average_shares)` — 開示時点の財務サマリー
- `jquants_master_snapshots(snapshot_date, ticker, name, market, sector_33, is_common_stock)` — as-of別の銘柄master
- `jpx_earnings_calendar(announcement_date, ticker)` — JPX決算予定
- `jquants_market_calendar(day, is_business_day)` — 営業日calendar
- `jquants_weekly_margin(week_end, ticker, long_vol, short_vol, long_std_vol, long_neg_vol, short_std_vol, short_neg_vol, issue_type)` — 全銘柄週次信用残
- `jquants_margin_alerts(publication_date, ticker, long_vol, short_vol, long_std_vol, long_neg_vol, short_std_vol, short_neg_vol, issue_type)` — 公表銘柄等の日次信用残
- `jquants_all_issues_daily_margin(balance_date, ticker, long_vol, short_vol, long_std_vol, long_neg_vol, short_std_vol, short_neg_vol, issue_type)` — 全銘柄日次信用残
- `jquants_short_sale_reports(disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name, discretionary_investment_contractor_name, investment_fund_name, short_ratio, short_shares, short_trading_units, previous_reported_at, previous_short_ratio, is_cancellation, notes)` — 0.5%以上の報告空売り残高
- `jpx_regulation_sources(asof_date, source_name, fetched_at_utc)` — JPX規制source取得事実
- `edinet_document_lists(doc_date, process_datetime, result_count, fetched_at_utc, is_final)` — EDINET document list取得事実
- `edinet_documents(doc_date, sequence_number, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, legal_status, disclosure_status, withdrawal_status, doc_info_edit_status, parent_doc_id, operation_datetime, submit_datetime, doc_description, period_start, period_end, edinet_code, issuer_edinet_code, subject_edinet_code)` — EDINET提出書類identityとlifecycle
- `tse_capital_policy_snapshots(snapshot_month_end, ticker, status, status_change, updated_on, contact_requested, first_disclosed_month_end, first_disclosure_left_censored)` — 東証資本コスト開示企業の月次PIT
- `jpx_delistings(delisted_on, ticker, name, market, reason)` — 上場廃止事実
- `tender_offer_exit_values(ticker, delisted_on, offer_price_yen, offer_doc_id, result_doc_id, filed_on)` — 成立した現金公開買付けの実現exit値
- `edinet_metrics(asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, investment_securities, ebitda_ttm, operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, equity, total_assets, consolidation_basis, source_doc_id, document_type, source_submit_datetime, source_period_start, source_period_end)` — EDINETから抽出した財務measurement
- `jpx_regulation_flags(asof_date, source_name, ticker, flag, fetched_at_utc)` — JPX規制flag
- `source_coverage(source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, status, error)` — fetch coverageとstatus
- `lake_store_origin(singleton, release_id, release_manifest_sha256)` — hydrateしたL1 releaseのidentity

## 検証

- rules/method hash一致
- 同一入力でReview Setがbyte-equivalent
- E[r]だけを変更してもmembership/order不変
- target未充足をdiagnosticsへ明示
- Research Triageの全entry一致、priority、Review Basis
- run store schema 5、application DB schema 19
- Web/APIがReview Set、Research Triage、Capital Allocation Assessmentを同じbindingで表示
