# components/candidates.md

Baibai-Loop 4 成分アーキテクチャの **(b) スクリーニング通過銘柄** の運用仕様。狭義のスクリーニング = 機械的ふるいの完了形を指す。全体構造は [`../architecture.md`](../architecture.md)、スクリーニング詳細は [`../screening/`](../screening/) 配下を参照。

## 1. 役割

- universe（日本株普通株、時価総額 100 億円以上、20 営業日平均売買代金 1 億円以上）に対し、複数の signal lane で機械的にふるいをかけ、**通過銘柄 list を事実として記録**
- 事実層のため解釈は入れない（反対仮説・原因仮説は research 側で行う）
- Micro track の出発点として、`records/04-research/` の選定入力となる

## 2. 頻度

- **定期**: 週次 1 回（初期値、retro で調整）
- 実行タイミング: 週初 / 週末の決まった曜日（例: 毎週月曜朝 or 金曜夕方）

## 3. Path と命名

```
records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル（週次運用のため）。

## 4. YAML 必須項目

```yaml
run_date: "YYYY-MM-DD"
asof_date: "YYYY-MM-DD"
universe_size: 整数
filters:
  min_market_cap_oku: 100
  min_avg_turnover_oku: 1.0
  exclude_listed_under_days: 182
generated_by: "screening-cli-v1"
data_sources:
  - "j-quants-light"
  - "jpx-public-regulation"
run_at: "ISO 8601"
run_id: "screening-YYYYMMDD-xxxxxxxx"
config_hash: "16 hex chars"
cache_manifest_hash: "16 hex chars"
candidates:
  - ticker: "130A"
    name: "..."
    sector_33: "輸送用機器"
    market_cap_oku: 1083
    avg_turnover_oku: 12.8
    per_forward: 8.2 | null
    per_trailing: 9.5
    pbr: 0.72
    ev_ebitda: 4.8 | null
    p_s: 0.6 | null
    pcfr: 5.1 | null
    price_change_60d: -0.155
    price_change_4w: -0.072
    sector_relative_strength_percentile: 0.35
    metrics:
      sales_ttm: 100000000000.0
      ocf_ttm: 13000000000.0
      edinet_ocf_ttm: 13000000000.0
      cash_to_market_cap: 0.42
      net_cash: null
      net_cash_to_market_cap: null
      price_to_equity: 0.82
      equity_ratio: 0.45
      ocf_yield: 0.13
      fcf_ttm: 8000000000.0
      fcf_yield: 0.08
      capex_ttm: 5000000000.0
      edinet_source_doc_id: S100XXXX
      edinet_document_type: "120"
      edinet_source_submit_datetime: "2026-04-01 12:00"
      edinet_source_period_start: "2025-04-01"
      edinet_source_period_end: "2026-03-31"
      edinet_capex_source: purchase_of_fixed_assets
      edinet_failure_reasons: debt_assumed_zero
      cfo_yoy: 0.08
      sales_yoy: 0.12
      operating_profit: 9000000000.0
      operating_profit_loss_narrowing: false
    metrics_breakdown:
      per_trailing:
        sector_median_gap: -0.21
        self_range_percentile: 0.14
        sigma_gap: -1.4
      pbr:
        sector_median_gap: -0.18
        self_range_percentile: 0.20
        sigma_gap: -1.1
      ev_ebitda:
        sector_median_gap: null
        self_range_percentile: null
        sigma_gap: null
      p_s:
        sector_median_gap: -0.35
        self_range_percentile: 0.18
        sigma_gap: -1.2
    ttm_quality:
      ev_ebitda: exact | approximated | unavailable
      p_s: exact | approximated | unavailable
      pcfr: exact | approximated | unavailable
      ocf_yield: exact | approximated | unavailable
      sales: exact | approximated | unavailable
      fcf_yield: exact | approximated | unavailable
      net_cash: exact | approximated | unavailable
    next_earnings_date: "YYYY-MM-DD" | null
    split_adjustment_flag: false
    signals:
      - name: valuation-reversion
        playbook: valuation-reversion
        reasons: [sector_self_range]
        metrics:
          condition_a_metric: per_trailing
          condition_a_sector_median_gap: -0.21
          condition_a_self_range_percentile: 0.14
signals_summary:
  valuation-reversion: 1
  strict-net-cash-discount: 0
  fcf-yield-discount: 0
  cash-rich-asset-discount: 0
  cashflow-yield-discount: 0
  sales-discount-growth: 0
```

- ticker は **4 文字の英数字文字列**として quote 必須（先頭 0 落ち防止、英字組入れ対応）
- 欠損値（例: forward EPS 未公表、EDINET 由来 EV/EBITDA 不在）は明示的に `null`
- `run_date` は `asof_date` と同値。ファイル path の日付とも一致させる
- `config_hash`: `ScreeningConfig` の secret 以外、provider URL、tier 設定、`records/_config/screening-rules.yaml` の内容 hash を含む
- `cache_manifest_hash`: `records/_data/raw/screening/` 配下の provider raw JSON cache の manifest hash
- `signals`: 通過した signal lane。複数 hit 可。表示順は rule config の lane 順に固定し、単一総合 score は持たせない
- `metrics`: candidate-level の flat な派生値。例: `sales_ttm`, `ocf_ttm`, `edinet_ocf_ttm`, `cash_to_market_cap`, `net_cash_to_market_cap`, `price_to_equity`, `equity_ratio`, `ocf_yield`, `fcf_yield`, `cfo_yoy`, `sales_yoy`, `operating_profit`, `edinet_source_doc_id`, `edinet_source_submit_datetime`, `edinet_source_period_start`, `edinet_source_period_end`, `edinet_failure_reasons`
- `ocf_ttm` は J-Quants 財務サマリーを TTM 正規化した営業 CF。`edinet_ocf_ttm` は EDINET CSV から抽出した CFO で、`fcf-yield-discount` の `fcf_ttm = edinet_ocf_ttm - capex_ttm` と同じ source family に属する
- `edinet_source_*`: EDINET CSV-derived metrics の提出書類 ID、doc type、提出日時、対象期間。research で一次資料へ戻るための traceability であり、strict net-cash / FCF signal の `signals[].metrics` にも同じ source metadata を入れる
- `metrics_breakdown`: valuation 指標ごとの `sector_median_gap` / `self_range_percentile` / `sigma_gap`
- `signals[].metrics`: signal hit の判定に直接使った値。valuation は `condition_a_metric` などの flat key、cash / CF / sales は lane 固有 key で記録する
- `ttm_quality`: `EV/EBITDA` / `P/S` / `PCFR` / `OCF yield` / `sales` / `FCF yield` / `net cash` の TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- `market_cap_oku` / `avg_turnover_oku`: research の position size と流動性確認で使う。universe 閾値は `market_cap_oku >= 100` かつ `avg_turnover_oku >= 1.0`
- `price_change_60d` / `price_change_4w`: split 影響を排除するため adjustment_close ベースで算出
- `split_adjustment_flag`: `price_change_60d` と同じ window 内に J-Quants `AdjustmentFactor` が株式分割 / 株式併合の調整を示した場合に `true`
- `sector_relative_strength_percentile`: **sector 単位の percentile**。銘柄個別の同業種内相対強度ではない

### 4.1 traceability の境界

candidates YAML は `run_id` / `config_hash` / `cache_manifest_hash` で実行時の input を追跡可能にする。ただし J-Quants Light tier は rolling 12 週間が取得上限のため、cache 中身が消えると過去データの再取得は不能。完全な point-in-time 再現性は本 repo のスコープ外とする。

### 4.2 実行メモの扱い

`records/03-candidates/` は YAML 正本とし、Markdown 本文は持たない。provider 状態、universe 除外件数、fallback / 部分警告は以下の配列フィールドで保持する。

- `fact_memo_lines`
- `provider_status_lines`
- `universe_exclusion_lines`
- `fallback_lines`
- `ttm_quality_counts`
- `signals_summary`

### 4.3 schema 検証

[`/records/_schemas/candidates-v1.json`](/records/_schemas/candidates-v1.json) が candidates YAML のコア schema (Draft 2020-12 jsonschema)。互換性を残さない方針のため、旧 schema の migration は持たない。手元では `uv run baibai-loop-validate --target candidates` で個別に走らせられる。

## 5. ワークフロー

1. 最新 universe を取得（J-Quants Light + JPX 除外条件適用）
2. 各 ticker の valuation / cash / CF / sales 指標を算出（[`../screening/valuation-metrics.md`](../screening/valuation-metrics.md) 参照）
3. signal lane（[`../screening/mechanical.md`](../screening/mechanical.md)）を適用
4. 通過銘柄を `candidates` 配列として YAML に記録
5. 補足情報（実行時の provider 状態、除外件数、fallback 等）を事実として配列フィールドに記録

## 6. research への接続

- `records/04-research/` の front matter `candidates_ref` で本ファイルを参照
- 選定プロセス: 最新 `records/03-candidates/` と最新 `records/02-outlook/` を突き合わせ、`outlook` で tailwind/neutral の業種/地域の ticker を候補に残す（headwind 除外）
- 複数 signal が重なる候補は research 優先度を上げるが、単一総合 score は作らない
- `select` は lane 別の primary metric と macro status を使って research triage を支援する。signal 数と時価総額だけでは並べない
- `select` output には `lane_toplists`、`ranked_candidates`、research 着手候補として lane 分散した `candidates` が含まれる。`ranked_candidates` は macro + lane rank + signal strength のグローバル順位、`candidates` は `output.research_selection_lane_order` に沿って各 lane の上位を重複排除した推奨リスト。`candidates` の件数は CLI `--top` と `output.research_selection_target_max` の小さい方、lane 別件数は `records/_config/screening-rules.yaml` の `output.lane_toplist_limit` で管理する
- 詳細: [`research.md`](./research.md) の選定プロセス
- research decision 後の追跡先: [`ledger.md`](./ledger.md)

## 7. 事実と分析の分離

- candidates は **事実層**。数値・signal hit 判定は機械的
- 「この銘柄は割安だ」という解釈は research 側で行う
- 「通過した」ことは事実だが、「採用すべき」は解釈

## 8. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| valuation / cash / CF / sales 指標の算出 | ○ | 異常値の手動確認 |
| signal hit 判定 | ○ | |
| YAML 整備 | ○ | |
| 数値ソースの一次確認 | ○ | 最終責任 |
| 最終 commit | | ○ |

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想（事実と分析の分離、マクロ優位）
- [`../architecture.md`](../architecture.md): 全体構造
- [`../screening/`](../screening/): スクリーニングサブシステム詳細
- [`../screening/universe-rules.md`](../screening/universe-rules.md): universe 境界条件
- [`../screening/valuation-metrics.md`](../screening/valuation-metrics.md): 指標算出仕様
- [`../screening/mechanical.md`](../screening/mechanical.md): 機械的ふるい仕様
- [`../templates/candidates.yaml`](../templates/candidates.yaml): template
