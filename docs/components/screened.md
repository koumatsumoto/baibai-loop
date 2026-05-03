# components/screened.md

Baibai-Loop 4 成分アーキテクチャの **(b) スクリーニング通過銘柄** の運用仕様。狭義のスクリーニング = 機械的ふるいの完了形を指す。全体構造は [`../architecture-v1.md`](../architecture-v1.md)、スクリーニング詳細は [`../screening/`](../screening/) 配下を参照。

## 1. 役割

- universe（日本株普通株、時価総額 200 億円以上、20 営業日平均売買代金 3 億円以上）に対し、valuation 指標でふるいをかけ、**通過銘柄 list を事実として記録**
- 事実層のため解釈は入れない（反対仮説・原因仮説は research 側で行う）
- Micro track の出発点として、`research/` の選定入力となる

## 2. 頻度

- **定期**: 週次 1 回（初期値、retro で調整）
- 実行タイミング: 週初 / 週末の決まった曜日（例: 毎週月曜朝 or 金曜夕方）

## 3. Path と命名

```
screened/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル（週次運用のため）。

## 4. YAML 必須項目

```yaml
run_date: "YYYY-MM-DD"              # 対象営業日（asof_date と同値）
asof_date: "YYYY-MM-DD"             # path の日付と同じ
universe_size: 整数                 # その時点の universe 銘柄数
filters:                            # 適用した閾値・条件
  min_market_cap_oku: 200
  min_avg_turnover_oku: 3
  # その他閾値
generated_by: "screening-cli-v1"
data_sources:
  - "j-quants-light"
  - "edinet-api-v2@2026-01-29"
  - "jpx-public-regulation"
run_at: "ISO 8601"
run_id: "screening-YYYYMMDD-xxxxxxxx"
config_hash: "16 hex chars"
cache_manifest_hash: "16 hex chars"
tickers:                            # 通過銘柄 list
  - ticker: "130A"
    name: "..."
    per_forward: 8.2 | null
    per_trailing: 9.5
    pbr: 0.72
    ev_ebitda: 4.8
    p_s: 0.6
    pcfr: 5.1
    sector_33: "輸送用機器"         # 東証 33 業種
    market_cap_oku: 1083            # 時価総額 (億円、整数)
    avg_turnover_oku: 12.8          # 20 営業日平均売買代金 (億円、小数 1)
    price_change_60d: -0.155        # 直近 60 営業日変化率 (adj close)
    price_change_4w: -0.072         # 直近 20 営業日変化率 (adj close)
    sector_relative_strength_percentile: 0.35
    metrics_breakdown:              # 各 valuation 指標の screening 用派生値
      per_trailing:
        sector_median_gap: -0.21    # 業種中央値比 (本人 / 中央値 - 1)
        self_range_percentile: 0.14 # 過去 750 日自己レンジ位置 [0=底, 1=頂]
        sigma_gap: -1.4             # 自己 history からの σ 偏差
      pbr:
        sector_median_gap: -0.18
        self_range_percentile: 0.20
        sigma_gap: -1.1
      ev_ebitda:                    # ttm_quality_ev_ebitda != exact なら null
        sector_median_gap: null
        self_range_percentile: null
        sigma_gap: null
    ttm_quality:
      ev_ebitda: exact | approximated | unavailable
      p_s: exact | approximated | unavailable
      pcfr: exact | approximated | unavailable
    next_earnings_date: "2026-05-13"  # asof 以降直近の決算発表日 (cache 範囲内、無ければ null)
    threshold_hit:                  # どの閾値条件を満たして通過したか（OR 条件）
      - sector_median_under_20pct_and_self_range_bottom_20pct
      - price_down_60d_and_valuation_sigma_down
      - sector_rotation_short_sell
```

- ticker は **4 文字の英数字文字列**として quote 必須（先頭 0 落ち防止、英字組入れ対応）
- 欠損値（例: forward EPS 未公表）は明示的に `null`
- `run_date` は `asof_date` と同値。ファイル path の日付とも一致させる
- `run_id`: 実行単位 ID。`screening-{asof_date:YYYYMMDD}-{config_hash 先頭 8 hex}` 形式
- `config_hash`: `ScreeningConfig` の secret 以外と provider URL / tier 設定を正規化した SHA256 短縮 hash。`--asof` や出力 path は含めない
- `cache_manifest_hash`: `data/raw/screening/` 配下の provider raw JSON cache（`manifests/` 除外）を path / sha256 / size で記録した manifest の SHA256 短縮 hash。配置先は `SCREENING_CACHE_DIR` で上書き可能（issue #45 で `.cache/screening` から git 管理対象の path に移行）
- `ttm_quality` は `EV/EBITDA` / `P/S` / `PCFR` の TTM 品質を `exact` / `approximated` / `unavailable` で明示する
- `threshold_hit`: mechanical-v1 の閾値条件 3 種のどれを満たしたか（OR 条件、複数 hit 可）
- `market_cap_oku` / `avg_turnover_oku`: research の position size 判定で使う。`market_cap_oku >= 200` かつ `avg_turnover_oku >= 3.0` で universe 通過する閾値と整合
- `price_change_60d` / `price_change_4w`: split 影響を排除するため adjustment_close ベースで算出。research §7 Price reaction の数値ソース
- `sector_relative_strength_percentile`: 4 週リターンの sector 内 percentile。条件 C の根拠
- `metrics_breakdown`: 各 valuation 指標 (per_trailing / pbr / ev_ebitda) の `sector_median_gap` / `self_range_percentile` / `sigma_gap` を集約。research §3 Valuation snapshot の primary metric 選択と判定根拠の数値ソース
- `next_earnings_date`: asof 以降直近の決算発表予定日 (J-Quants earnings calendar、asof + 90 calendar days 範囲内)。research §10 Entry 条件の「決算またぎ kill switch」自動 check に使う。範囲内に予定が無い銘柄は `null`

### 4.1 traceability の境界

screened YAML は `run_id` / `config_hash` / `cache_manifest_hash` で実行時の input を追跡可能にする。ただし J-Quants Light tier は rolling 12 週間が取得上限のため、cache 中身が消えると過去データの再取得は不能。完全な point-in-time 再現性は本 repo のスコープ外とする。

### 4.2 実行メモの扱い

`screened/` は YAML 正本とし、Markdown 本文は持たない。複数閾値 hit、provider 状態、universe 除外件数、fallback / 部分警告は以下の配列フィールドで保持する。

- `fact_memo_lines`
- `provider_status_lines`
- `universe_exclusion_lines`
- `fallback_lines`
- `ttm_quality_counts`

### 4.3 schema 検証

[`../../schemas/screened-v1.json`](../../schemas/screened-v1.json) が screened YAML のコア schema (Draft 2020-12 jsonschema)。`baibai-loop-validate` CLI が同 schema で全 `screened/*.yaml` を検査し、CI の `Validate artefacts` step で merge gate になる。手元では `uv run baibai-loop-validate --target screened` で個別に走らせられる。

## 5. ワークフロー

### 5.1 実行手順

1. 最新 universe を取得（J-Quants Light + JPX 除外条件適用）
2. 各 ticker の valuation 指標を算出（[`../screening/valuation-metrics.md`](../screening/valuation-metrics.md) 参照）
3. 閾値条件（[`../screening/mechanical-v1.md`](../screening/mechanical-v1.md) の 3 種 OR）を適用
4. 通過銘柄を `tickers` 配列として YAML に記録
5. 補足情報（実行時の provider 状態、除外件数、fallback 等）を事実として配列フィールドに記録

### 5.2 実装

- automation v1 は `python -m baibai_loop.screening.cli run --asof YYYY-MM-DD` を正本とする
- raw cache の事前取得は `python -m baibai_loop.screening.cli bootstrap-cache --start YYYY-MM-DD --end YYYY-MM-DD` を使う
- automation の正本設計は [`../screening/automation-v1.md`](../screening/automation-v1.md) を参照
- CLI 化後も、人間が異常値 spot check してから commit する

## 6. research への接続

- `research/` の front matter `screened_ref` で本ファイルを参照
- 選定プロセス: 最新 `screened/` と最新 `view/` を突き合わせ、`view` で tailwind/neutral の業種/地域の ticker を候補に残す（headwind 除外）
- 詳細: [`research.md`](./research.md) の選定プロセス
- research decision 後の追跡先: [`ledger.md`](./ledger.md)

## 7. 事実と分析の分離

- screened は **事実層**。valuation 数値・閾値 hit 判定は機械的
- 「この銘柄は割安だ」という解釈は research 側で行う
- 「通過した」ことは事実だが、「採用すべき」は解釈

## 8. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| valuation 指標の算出 | ○ | 異常値の手動確認 |
| 閾値適用・threshold_hit 判定 | ○ | |
| YAML 整備 | ○ | |
| 数値ソースの一次確認 | ○ | 最終責任 |
| 最終 commit | | ○ |

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想（事実と分析の分離、マクロ優位）
- [`../architecture-v1.md`](../architecture-v1.md): 全体構造
- [`../screening/`](../screening/): スクリーニングサブシステム詳細
- [`../screening/universe-rules.md`](../screening/universe-rules.md): universe 境界条件
- [`../screening/valuation-metrics.md`](../screening/valuation-metrics.md): 指標算出仕様
- [`../screening/mechanical-v1.md`](../screening/mechanical-v1.md): 機械的ふるい仕様（閾値 3 種 OR）
- [`../templates/screened.yaml`](../templates/screened.yaml): template
