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
uv run baibai-engine screening run --asof YYYY-MM-DD [--runs-db PATH] \
  [--run-revision-id ID --run-at TIMESTAMP]
uv run baibai-engine screening review-set publish --asof YYYY-MM-DD \
  --run-revision-id ID [--runs-db PATH] [--output-path PATH] [--format yaml|json] \
  [--review-set-id ID --created-at TIMESTAMP]
uv run baibai-engine screening review-set show --review-set-id ID \
  [--runs-db PATH] [--output-path PATH] [--force] [--format yaml|json]
uv run baibai-engine screening research-triage publish DRAFT.yaml \
  [--db PATH] [--runs-db PATH]
uv run baibai-engine screening prune --keep N [--runs-db PATH]
```

`run`のexit 2はpublication済みpartial warningである。warningを確認してから同じ`run_revision_id`で後続へ進む。Review Setの再表示に再発行を使わない。`--run-revision-id` + `--run-at`と`--review-set-id` + `--created-at`はcallerがidentityを固定する場合のsame-ID idempotencyを提供する。通常のcloud dailyとlocal `analysis run`はserver-generated identityを使い、AI outputからIDやclockを受け取らない。

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

publisherはsource run、as-of、rules hash、method hash、全Security Analysisからpayloadを再計算し、不一致を拒否する。published rootは`screening_rules_hash`をprovenanceとして持つ。Review Setはapplication DBやResearch Triageを読まず、同じrun・rules・implementationから同じcompositionを再構築するL2である。短期run cacheに置き、日次membership履歴を別保存しない。

Normalized Earnings Powerのnative eligibility/orderは`normalized_per_3fy`と同sector gapを使う。FV/E[r] estimatorは`normalized_per_3fy`を入力にしない。この境界はReview Set composerの変更ではない。

## Research Triage v2

application DB schema v20の`research_triage`はReview Set全entryをexactly onceで保持する。current writerはv2だけを発行し、既存v1 rowはimmutable historyとしてread modelだけが投影する。

- `research`: contiguousな`priority`、`rationale`、`research_question`、`key_risk`が必須
- `skip`: `rationale`が必須で、`priority`、`research_question`、`key_risk`は禁止

manual scaffoldはReview Set IDからrun storeのcanonical publicationを解決し、Review Setのas-of以下で最新のMacro Contextと、application DBのlatest Triage IDを取得する。通常の`analysis run`はeditable scaffoldを介さず、strictなjudgment fieldから同じdomain objectを組み立てる。publisherは`review_set_id`、`run_revision_id`、`as_of`、`screening_rules_hash`、Candidate Discovery method、全tickerを検証し、`candidate_snapshot`をsource Review Setから上書きする。snapshotはidentity、`review_position`、`nominations`、grouped `analysis`を持ち、`support_count`はnominations数から導出する。`rank_vector`や単独`fair_value`は複写しない。

global headは`as_of DESC, julianday(published_at) DESC, research_triage_id DESC`の実時刻total orderで決める。`expected_prior_research_triage_id`はpublish transaction内でこのheadとCASする。新規publicationは`as_of`を後退させず、awareな`published_at`を現headより進め、未来時刻またはJST換算日が`as_of`より前の時刻を使わない。同じIDと同じcanonical payloadの再送だけは、後続headの有無にかかわらず冪等に成功する。これにより全てのnon-idempotent publicationが新headになり、同じpriorから分岐したdraftを拒否する。

Contextが無ければ`null`は正常、古ければwarningであり、どちらもReview Setのnomination、membership、orderを変えない。eligible Contextがあるのに`null`は拒否する。明示的に古いeligible revisionを選ぶことはできるが、選択理由を既存の判断文へ残す。発行後payloadはimmutableである。`rationale`、および非`null`の`research_question` / `key_risk`は空白だけの値を拒否するが、検証時に前後空白を書き換えない。

Research TriageはResearch Setのadmission可能範囲を定める。人間は`research` entryの部分集合だけをResearch Setとして確定できる。`research prepare --research-triage-id`はas-ofと比較snapshotをv2 payloadから導出し、Review Set fileやrun storeを要求しない。manifestはTriage ID、canonical payload hash、ledger append headを持ち、status・scaffold・promoteを含む各gateがapplication DBを再読してhashとresearchable ticker集合を検証する。

E[r] calibration contextは共有read contractがartifact schema、generated/expiry、3y/5y、quantile/band、rules hash、E[r] model versionを検証する。ResearchはTriage rootのrules hashとcandidate `analysis.expected_return`のmodel/version・ratio-valued `er_annual`を使い、利用不能理由を明示する。これはhistorical contextで、membership/order、Triage、FV、buy judgmentへ伝播しない。

## Daily batchとlocal analysis

cloud dailyはL1 / L2 machine処理だけを自動実行する。

```text
screening run -> review-set publish -> web materialize -> publish
```

cloudからL3 Research Triageを発行しない。local `baibai-batch analysis run`は同じdaily jobを再利用した後、AI不要条件をmachineで確定し、必要な場合だけReview Set全体を原則1 requestで分類してResearch Triageを発行する。Research Set確定とCapital Allocation Assessmentは人間gateの後に残す。full-depth Macro Context、migration、全期間再取得はどちらの日次経路にも載せない。

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
- Research Triageの全entry一致、priority、rules/method identity、expected prior ID
- 非営業日、空Review Set、active Operation、exact既存Triageでmodel process 0
- 通常Review Setは共有Macro projectionを1回だけ含む1 AI request、invalid resultはcanonical write 0
- 全件skipはOperation 0、researchありはexact Triageを参照するOperation 1
- run store schema 5、application DB schema 20
- Web/APIがReview Set、Research Triage、Capital Allocation Assessmentを同じbindingで表示
