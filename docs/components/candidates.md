# components/candidates.md

Baibai-Loop の **screen output / candidates** の運用仕様。狭義のスクリーニングは、機械的ふるいの完了形を指す。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md)、スクリーニング詳細は [`../screening/`](../screening/) 配下を参照。

## 1. 役割

- universe に対し、複数の playbook-linked screen で機械的にふるいをかけ、ticker-level の raw screen output を事実として記録する
- 事実層のため、反対仮説・原因仮説・採用判断は書かない
- `records/05-research/` の出発点として使う
- Macro context は candidates に保存せず、`select` と research の `macro_context_fit` で扱う

## 2. Path と永続化

```text
records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル。週次運用を基本にする。

candidates YAML は **git に積まない local store** として扱う(`.gitignore` 対象)。candidates は L1 SQLite からの決定論的な L2 機械出力であり、全銘柄スコープ化(1 ファイル数 MB)以降、git 履歴に積む価値がない。一方で forward 計測(replay / lane cohorts / missed-opportunity tracking)と `candidate_ref` の lineage 検証は過去ファイルを必要とするため、**履歴はこの path にローカル保持し続け、`data/screening/market.sqlite` と同じ local-canonical 扱いにする**(バックアップを取る場合は両方を対象にする)。

candidates ファイルは git に一切置かない。`candidate_ref` は record 内の補助ポインタであり、ファイル横断の lineage 検証は行わない(監査証跡を保持しない方針)。

## 3. YAML Contract

下は最小 contract と主要 field の例。完全な contract は [`../../records/_schemas/candidates.json`](../../records/_schemas/candidates.json) と実 record を正本にし、[`../templates/candidates.yaml`](../templates/candidates.yaml) は手で形を確認するための最小例として扱う。現行 CLI は J-Quants / JPX に加え、利用可能な場合は EDINET preprocessed metrics、JPX public regulation、disclosure title events、sector relative strength、cash / asset / equity / capex 由来の派生指標を追加する。

```yaml
run_date: "YYYY-MM-DD"
asof_date: "YYYY-MM-DD"
universe_size: 3744
filters:
  scope: all-common-stocks
  markets: prime/standard/growth
  min_bar_history: 20
generated_by: "screening-cli-v1"
data_sources:
  - "j-quants-light"
  - "edinet-preprocessed-metrics"
  - "jpx-public-regulation"
  - "disclosure-title-events"
run_at: "ISO 8601"
run_id: "screening-YYYYMMDD"
candidates:
  - ticker: "130A"
    name: "..."
    sector_33: "輸送用機器"
    market_cap_oku: 1083
    avg_turnover_oku: 12.8
    listing_span_days: 1200
    jpx_flags: []
    per_forward: 8.2
    per_trailing: 9.5
    pbr: 0.72
    ev_ebitda: 4.8
    p_s: 0.6
    pcfr: 5.1
    price_change_1d: -0.018
    price_change_5d: -0.082
    price_change_20d: -0.118
    price_change_60d: -0.155
    sector_relative_strength_percentile: 0.35
    gap_from_52w_low: 0.07
    turnover_spike_5d: 2.4
    metrics:
      sales_ttm: 100000000000.0
      ocf_ttm: 13000000000.0
      cash_to_market_cap: 0.42
      net_cash_to_market_cap: null
      price_to_equity: 0.82
      equity_ratio: 0.45
      ocf_yield: 0.13
      fcf_yield: 0.08
      cfo_yoy: 0.08
      sales_yoy: 0.12
      operating_profit: 9000000000.0
      edinet_ocf_ttm: 13000000000.0
      cash_eq: 48000000000.0
      total_assets: 160000000000.0
      equity: 72000000000.0
      capex_ttm: 5000000000.0
    ttm_quality:
      ev_ebitda: exact
      p_s: exact
      pcfr: unavailable
      ocf_yield: exact
      sales: exact
      fcf_yield: approximated
      net_cash: approximated
    next_earnings_date: "YYYY-MM-DD"
    split_adjustment_flag: false
    freshness_warnings: []
    evidence_hits:
      - name: valuation-reversion
        playbook_id: valuation-reversion
        source_status: ok
        sizing_eligible: true
        reasons: [sector_self_range]
        metrics:
          condition_a_metric: per_trailing
          condition_a_sector_median_gap: -0.21
          condition_a_self_range_percentile: 0.14
evidence_hits_summary:
  valuation-reversion: 1
```

- ticker は 4 文字の英数字文字列として quote 必須
- 欠損値は `null` で明示する
- `evidence_hits` は通過した playbook-linked screen。複数 hit 可。単一総合 score は持たせない
- `metrics` は research で再利用する flat な派生値
- EDINET / disclosure title events 由来の field は取得できたときだけ出る。欠損時に placeholder を足さない
- `ttm_quality` は主要 TTM 指標の品質を `exact | approximated | unavailable` で示す
- `freshness_warnings` は EDINET metrics の後に重要開示がある場合の再確認メモ

## 4. Traceability

候補母集団の確認は `universe_size`、SQLite coverage、git 履歴で行う。Research では `candidate_ref.candidates_ref` と `candidate_ref.ticker` で候補行へ戻る。

## 5. Research への接続

- `records/05-research/` の `candidate_ref.candidates_ref` で candidates file を参照する
- `candidate_ref.ticker` と candidates row の `ticker` を照合する
- `select` は candidates と macro context を突き合わせ、`recommendations` と `selection.diagnostics` を出す
- `recommendations` は research 着手候補。default summary では `selection_lane`、macro alignment、long-hold rating、reason / risk tags を見て深掘り順を決める。full lens / debug detail が必要な場合は `select --detail full` を使う
- `select-sweep` は `balanced` と `--profile-config` で定義した任意 profile を比較し、recommended tickers、fast count、long-hold count、suppressed count、previous overlap、sector / lane concentration、profile diff を確認する

## 6. 事実と分析の分離

- candidates は事実層。数値・screen hit 判定は機械的
- 「この銘柄は割安だ」という解釈は research 側で行う
- 「通過した」ことは事実だが、「採用すべき」は解釈
