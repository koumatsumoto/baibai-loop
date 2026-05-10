# components/candidates.md

Baibai-Loop の **screen output / candidates** の運用仕様。狭義のスクリーニング = 機械的ふるいの完了形を指す。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md)、概念モデルは [`../concepts.md`](../concepts.md)、スクリーニング詳細は [`../screening/`](../screening/) 配下を参照。

## 1. 役割

- universe（日本株普通株、時価総額 100 億円以上、20 営業日平均売買代金 1 億円以上）に対し、複数の playbook-linked screen で機械的にふるいをかけ、**ticker-level の raw screen output を事実として記録**
- 事実層のため解釈は入れない（反対仮説・原因仮説は research 側で行う）
- Investment memo の出発点として、`records/05-research/` の選定入力となる
- Playbook hit、policy / liquidity / macro regime gate の初期結果は screen fact として残す。後続の選定・見送り・保留判断は candidates を上書きせず、research / ledger / review 側の記録で追跡する
- candidates の `macro_regime_gate_result` は screening 実行時の pinned rules / input に基づく fact field。最新 outlook との投資判断上の整合は `select` と research の責務であり、candidates YAML を後から書き換えて揃えない

## 2. 頻度

- **定期**: 週次 1 回（初期値、retro で調整）
- 実行タイミング: 週初 / 週末の決まった曜日（例: 毎週月曜朝 or 金曜夕方）

## 3. Path と命名

```
records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml
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
run_id: "screening-YYYYMMDD"
candidates:
  - ticker: "130A"
    candidate_id: candidate-YYYY-MM-DD-130A
    candidate_key: "screening-YYYYMMDD:130A"
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
    freshness_warnings:
      - source_family: "edinet-metrics"
        stale_metric: "edinet_metrics"
        reason: "material_event_after_edinet_source"
        event_date: "2026-03-03"
        event_kind: "borrowing"
        event_title: "資金の借入に関するお知らせ"
        event_source: "tdnet-title-cache"
        event_url: null
        edinet_source_submit_datetime: "2025-10-15 15:00"
    evidence_hits:
      - name: valuation-reversion
        playbook_id: valuation-reversion
        evidence_hit_id: eh-130A-valuation-reversion
        primary_family: valuation
        evidence_family_set: [valuation]
        correlation_group: valuation_discount
        independence_component_id: valuation_discount
        source_status: ok
        sizing_eligible: true
        reasons: [sector_self_range]
        metrics:
          condition_a_metric: per_trailing
          condition_a_sector_median_gap: -0.21
          condition_a_self_range_percentile: 0.14
evidence_hits_summary:
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
- screening rules / metric catalog / policy は git 管理ファイルそのものを正本にし、candidates YAML には content hash snapshot を持たせない
- `evidence_hits`: 通過した playbook-linked screen を表す field。概念上は evidence hit として扱う。複数 hit 可。表示順は rule config の lane 順に固定し、単一総合 score は持たせない
- `metrics`: candidate-level の flat な派生値。例: `sales_ttm`, `ocf_ttm`, `edinet_ocf_ttm`, `cash_to_market_cap`, `net_cash_to_market_cap`, `price_to_equity`, `equity_ratio`, `ocf_yield`, `fcf_yield`, `cfo_yoy`, `sales_yoy`, `operating_profit`, `edinet_source_doc_id`, `edinet_source_submit_datetime`, `edinet_source_period_start`, `edinet_source_period_end`, `edinet_failure_reasons`
- `ocf_ttm` は J-Quants 財務サマリーを TTM 正規化した営業 CF。`edinet_ocf_ttm` は EDINET CSV から抽出した CFO で、`fcf-yield-discount` の `fcf_ttm = edinet_ocf_ttm - capex_ttm` と同じ source family に属する
- `edinet_source_*`: EDINET CSV-derived metrics の提出書類 ID、doc type、提出日時、書類 metadata 上の対象期間。research で一次資料へ戻るための traceability であり、strict net-cash / FCF screen hit の `evidence_hits[].metrics` にも同じ source metadata を入れる。半期報告書 / 訂正半期報告書では `source_period_end` が fiscal year end を指すことがあるため、FCF / CFO の測定期間そのものとは限らない
- `metrics_breakdown`: valuation 指標ごとの `sector_median_gap` / `self_range_percentile` / `sigma_gap`
- `evidence_hits[].metrics`: screen hit の判定に直接使った値。valuation は `condition_a_metric` などの flat key、cash / CF / sales は lane 固有 key で記録する
- `ttm_quality`: `EV/EBITDA` / `P/S` / `PCFR` / `OCF yield` / `sales` / `FCF yield` / `net cash` の TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- `market_cap_oku` / `avg_turnover_oku`: research の position size と流動性確認で使う。universe 閾値は `market_cap_oku >= 100` かつ `avg_turnover_oku >= 1.0`
- `price_change_60d` / `price_change_4w`: split 影響を排除するため adjustment_close ベースで算出
- `split_adjustment_flag`: `price_change_60d` と同じ window 内に J-Quants `AdjustmentFactor` が株式分割 / 株式併合の調整を示した場合に `true`
- `freshness_warnings`: EDINET CSV-derived metrics の提出日以降、候補 `asof_date` までに任意の disclosure title cache (`.cache/screening/disclosures/**/*.json`) から M&A / 借入 / 社債 / 自己株買い / 設備投資 / 増資 / 減資 / 資本業務提携系の title keyword hit が見つかった場合に出す。同日開示は時刻順を判定できないため保守的に warning 対象に含める。`stale_metric: edinet_metrics` は net cash だけでなく cash / debt / EV / equity / share count / FCF など EDINET-derived metrics 全体の再確認が必要であることを示す。cache が無い場合は provider_status_lines で optional unavailable として明示する
- `sector_relative_strength_percentile`: **sector 単位の percentile**。銘柄個別の同業種内相対強度ではない

### 4.1 traceability の境界

candidates YAML は `run_id` と candidate-level の metric / source metadata を記録する。SQLite の厳密な point-in-time hash audit や policy snapshot は保持しない。必要な運用確認は git 履歴、SQLite coverage 検証、research 時の一次情報確認で行う。

### 4.2 実行メモの扱い

`records/04-candidates/` は YAML 正本とし、Markdown 本文は持たない。provider 状態、universe 除外件数、fallback / 部分警告は以下の配列フィールドで保持する。

- `fact_memo_lines`
- `provider_status_lines`
- `universe_exclusion_lines`
- `fallback_lines`
- `ttm_quality_counts`
- `evidence_hits_summary`

### 4.3 schema 検証

[`/records/_schemas/candidates.json`](/records/_schemas/candidates.json) が candidates YAML のコア schema (Draft 2020-12 jsonschema)。手元では `uv run baibai-loop-validate --target candidates` で個別に走らせられる。

## 5. ワークフロー

1. 最新 universe を取得（J-Quants Light + JPX 除外条件適用）
2. 各 ticker の valuation / cash / CF / sales 指標を算出（[`../screening/valuation-metrics.md`](../screening/valuation-metrics.md) 参照）
3. playbook-linked screen rules（[`../screening/mechanical.md`](../screening/mechanical.md)）を適用
4. 通過銘柄を `candidates` 配列として YAML に記録
5. 補足情報（実行時の provider 状態、除外件数、fallback 等）を事実として配列フィールドに記録

## 6. research への接続

- `records/05-research/` の front matter `candidates_ref` で本ファイルを参照
- 選定プロセス: 最新 `records/04-candidates/` と最新 `records/03-outlook/` を突き合わせ、`outlook` で supportive/neutral の業種/地域の ticker を候補に残す（adverse 除外）
- 複数 screen hit が重なる候補は research 優先度を上げるが、単一総合 score は作らない
- `select` は lane 別の primary metric と macro status を使って research triage を支援する。hit 数と時価総額だけでは並べない
- `select` output には `lane_toplists`、`ranked_candidates`、research 着手候補として lane 分散した `candidates` が含まれる。`ranked_candidates` は macro + lane rank + evidence strength のグローバル順位、`candidates` は `output.research_selection_lane_order` に沿って各 lane の上位を重複排除した推奨リスト。`candidates[].recommendation_lane` は lane 分散でその候補を拾った枠、`candidates[].selection_lane` は primary thesis として優先確認する screen。複数 hit 銘柄では両者が異なることがある。`candidates` の件数は CLI `--top` と `output.research_selection_target_max` の小さい方、lane 別件数は `records/_config/screening-rules/2026-05-01T000000+0900.yaml` の `output.lane_toplist_limit` で管理する
- `selected` という語は `select` output の research triage queue だけを指す。raw candidates の row flag ではなく、research approval でも order ready でもない。段階は `screening_selected` → `research_memo` / `candidate_screen.not_reviewed` → `research_approved` → `order_ready` と分けて読む
- candidates validator は row が orthogonal gate fields を持つことを検査する。Outlook と macro reducer の最終整合は research validator が検査する
- 詳細: [`research.md`](./research.md) の選定プロセス
- research decision 後の追跡先: [`ledger.md`](./ledger.md)

## 7. 事実と分析の分離

- candidates は **事実層**。数値・screen hit 判定は機械的
- 「この銘柄は割安だ」という解釈は research 側で行う
- 「通過した」ことは事実だが、「採用すべき」は解釈

## 8. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| valuation / cash / CF / sales 指標の算出 | ○ | 異常値の手動確認 |
| screen hit 判定 | ○ | |
| YAML 整備 | ○ | |
| 数値ソースの一次確認 | ○ | 最終責任 |
| 最終 commit | | ○ |

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想（事実と分析の分離、macro regime discipline）
- [`../architecture/system-overview.md`](../architecture/system-overview.md): 全体構造
- [`../concepts.md`](../concepts.md): 投資判断ドメインモデル
- [`../screening/`](../screening/): スクリーニングサブシステム詳細
- [`../screening/universe-rules.md`](../screening/universe-rules.md): universe 境界条件
- [`../screening/valuation-metrics.md`](../screening/valuation-metrics.md): 指標算出仕様
- [`../screening/mechanical.md`](../screening/mechanical.md): 機械的ふるい仕様
- [`../templates/candidates.yaml`](../templates/candidates.yaml): template
