# architecture

Baibai-Loop の **構造・schema・procedure** を記述する正本。
思想・ベースの考え方は [`philosophy.md`](./philosophy.md) に分離する。

## 1. アーキテクチャ概観

Baibai-Loop は **4 成分 + 下流 2 成分** で構成される意思決定ループ基盤である。

| 成分 | directory | 役割 | 頻度 |
| --- | --- | --- | --- |
| **a** | [`records/01-brief/`](../records/01-brief/) | マクロ事実ブリーフ | 定期（週次/月次）+ 不定期 |
| **b** | `records/03-candidates/` | スクリーニング通過銘柄 | 定期（週次） |
| **c** | `records/02-outlook/` | マクロ見解 | 定期（月次）+ 不定期 |
| **d** | `records/04-research/` | 個別銘柄リサーチ（packet 本体） | candidates 後の選定単位 |
| ― | `records/05-trades/` | 執行記録 | 採用時 |
| ― | `records/06-reviews/` | 事後検証 | 決済後 +15/+30 営業日 + 月次 retro |

運用成果物と証跡は `records/` 配下に集約する。`docs/`, `src/`, `tests/`, `.github/` は仕様・実装・検証基盤として root に残す。

## 2. 全体ワークフロー（2 トラック + 統合 + フィードバック）

```
┌─────────────── Macro Track (independent) ───────────────┐
│                                                          │
│   primary sources (BOJ/BLS/JPX 等)                       │
│             │                                            │
│             ↓                                            │
│    ┌────────────┐    積み上げ    ┌────────────┐          │
│    │ records/   │ ─────────────→ │ records/   │          │
│    │ 01-brief/  │                │ 02-outlook/│          │
│    │ (a) 事実   │                │ (c) 見解   │          │
│    └────────────┘                └─────┬──────┘          │
│                                        │                 │
└────────────────────────────────────────┼─────────────────┘
                                         │ outlook_ref (required)
┌─────────────── Micro Track ────────────┼─────────────────┐
│                                        │                 │
│   primary sources (J-Quants 等)        │                 │
│             │                          │                 │
│             ↓                          │                 │
│    ┌──────────────┐                    │                 │
│    │ records/     │                    │                 │
│    │ 03-candidates│                    │                 │
│    │ (b) ふるい   │                    │                 │
│    └──────┬───────┘                    │                 │
│           │ candidates_ref (required)  │                 │
│           └──────────┬─────────────────┘                 │
│                      ↓                                   │
│              ┌─────────────┐                             │
│              │ records/    │                             │
│              │ 04-research │                             │
│              │ (d) 深掘り  │                             │
│              └──────┬──────┘                             │
│                     │ 採用判定                           │
│                     ↓                                    │
│              ┌─────────────┐                             │
│              │ records/    │                             │
│              │ 05-trades/  │                             │
│              └──────┬──────┘                             │
│                     ↓                                    │
│              ┌─────────────┐                             │
│              │ records/    │                             │
│              │ 06-reviews/ │                             │
│              └──────┬──────┘                             │
│                     │                                    │
│                     ↓ retro                              │
│              feedback → playbook / screening 改訂        │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## 3. 各成分の仕様

### 3.1 `records/01-brief/` — (a) マクロ事実ブリーフ

#### 3.1.1 責務

- 世界情勢・日本経済・業種動向の **一次情報** を短く記録
- **独立トラック**: 売買ループから独立して積み上がる

#### 3.1.2 種類

| type | 頻度 | trigger | 典型例 |
| --- | --- | --- | --- |
| `periodic` | 日次 / 週次 / 月次 | 定期 | `world-daily-YYYY-MM-DD-*.yaml`, `world-weekly-YYYY-MM-DD-*.yaml`, `YYYY-MM-macro-monthly-*.yaml` |
| `event` | 不定期 | 重大イベント | BOJ / FOMC / CPI 大振れ / 地政学 shock |

**不定期 trigger 閾値**:

- 主要統計が予想対比 ±10% 以上乖離
- 金融政策変更（利上げ・利下げ、YCC 等）
- 主要指数 ±3% 以上変動（Nikkei 225, S&P 500 等）

`world-daily` は `world-weekly` と `macro-monthly` の間を埋める freshness bridge として扱う。単一統計の通常公表は、`macro-monthly` が未作成ならまず `world-daily` に載せ、上記閾値を満たす decisive event のときだけ `event` kind を使う。

#### 3.1.3 Path

```
records/01-brief/YYYY/MM/YYYY-MM-DD-{kind}-{slug}.yaml
```

- `{kind}` 例: `world-daily` / `world-weekly` / `macro-monthly` / `fomc` / `boj` / `cpi` / `gdp` / `geopolitics` / `event`
- `{slug}`: 内容を端的に示す短い英小文字ハイフン区切り（解釈語は避ける）

#### 3.1.4 YAML

```yaml
schema_version: 1
kind: world-weekly | world-daily | macro-monthly | fomc | boj | cpi | gdp | geopolitics | event
type: periodic | event
scope: world | japan | sector-xx
ai_draft: true | false
published_at: "ISO 8601"
observation_date: "YYYY-MM-DD"
period:                       # world-weekly では必須、daily では任意
  start: "YYYY-MM-DD"
  end: "YYYY-MM-DD"
  market_basis_date: "YYYY-MM-DD"
month: "YYYY-MM"              # macro-monthly では必須
references:
  prev_period: records/01-brief/YYYY/MM/...yaml | null
  latest_monthly: records/01-brief/YYYY/MM/...yaml | null
sources:
  - id: <id>
    name: <ソース名>
    url: <URL>
    accessed_at: "YYYY-MM-DD"
    status: ok | partial | failed
layers:
  world: { market_indicators: [...], events: [...], ... }
  japan: { fx: [...], events: [...], ... }
  japan_equity: { market_indicators: [...], sector_movements: [...], events: [...] }
deltas:                       # world-weekly / macro-monthly では必須
  threshold_breaches: [...]
  unjudgeable: [...]
  direction_history: [...]
  direction_reversals: [...]
fact_memos: [...]             # 任意
next_events: [...]
```

- `sources[].id` は brief 内の安全な ID。本文の各 item は `source_ids: [<id>, ...]` で参照する
- `layers` は `world` / `japan` / `japan_equity` の 3 キーを必ず持つ。該当なしは `{}` を明示
- 詳細は [`components/brief.md`](./components/brief.md) と [`../records/_schemas/brief-v1.json`](../records/_schemas/brief-v1.json) を参照

### 3.2 `records/03-candidates/` — (b) スクリーニング通過銘柄

#### 3.2.1 責務

- universe × valuation 指標で機械的にふるいをかけ、**通過銘柄の list を事実として記録**
- 解釈はここに入れない

#### 3.2.2 頻度

- 定期: 週次（初期値、retro で調整）

#### 3.2.3 Path

```
records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル。

#### 3.2.4 YAML

詳細は [`components/candidates.md`](./components/candidates.md) §4 を正本とする。要点のみ抜粋:

- `run_date` / `asof_date` (同値) / `universe_size` / `filters`
- `generated_by`, `data_sources`, `run_at`
- `tickers[]`: `ticker`, `name`, valuation 指標 (`per_forward`, `per_trailing`, `pbr`, `ev_ebitda`, `p_s`, `pcfr`), `sector_33`, `ttm_quality`, `threshold_hit`

### 3.3 `records/02-outlook/` — (c) マクロ見解

#### 3.3.1 責務

- `records/01-brief/` の積み上げを source として、業種/地域/資産クラス別の追い風/中立/逆風評価を生成
- `records/04-research/` の Macro gate 判定で参照される唯一の source

#### 3.3.2 更新 trigger

- **定期**: 月次 1 回（月初 3 営業日以内）
- **不定期**: BOJ 会合決定、FOMC 決定、CPI 大振れ（予想対比 ±0.5% 以上）、主要指数 ±5% 以上変動時

#### 3.3.3 Path

```
records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-<slug>.yaml
```

#### 3.3.4 YAML

```yaml
schema_version: 1
ai_draft: true | false
published_at: "ISO 8601"
horizon: "1-6m"
updated_from:
  - records/01-brief/YYYY/MM/...yaml
summary: <1 段落の要約>
sectors:                        # 東証 33 業種を全件必須
  "水産・農林業":
    status: tailwind | neutral | headwind | null
    rationale: <判定根拠>
    source_refs: [records/01-brief/...yaml]
  ...
regions:                        # 4 地域を全件必須
  us:           { status: ..., rationale: ..., source_refs: [...] }
  japan-domestic: { status: ..., rationale: ..., source_refs: [...] }
  japan-external-demand: { status: ..., rationale: ..., source_refs: [...] }
  emerging:     { status: ..., rationale: ..., source_refs: [...] }
changes:
  - target: <sector or region>
    from_status: ...
    to_status: ...
    rationale: ...
    source_refs: [...]
next_triggers:
  - date: "YYYY-MM-DD"
    text: "<イベント>"
```

- `sectors` は東証 33 業種を全件必須にする。変動がない業種は `status: neutral` でも明示する
- `regions` は `us` / `japan-domestic` / `japan-external-demand` / `emerging` の 4 件を全件必須にする
- `status` の許容値は `tailwind` / `neutral` / `headwind` / `null` のみ。`null` の場合も `rationale` は必須
- 詳細は [`components/outlook.md`](./components/outlook.md) と [`../records/_schemas/outlook-v1.json`](../records/_schemas/outlook-v1.json) を参照

### 3.4 `records/04-research/` — (d) 個別銘柄リサーチ packet

#### 3.4.1 責務

- `records/03-candidates/` × `records/02-outlook/` から選定した個別銘柄の深掘り + 採用判定
- 4 成分統合の出力 = packet 本体

#### 3.4.2 選定プロセス（candidates × outlook → 候補絞り込み）

1. 最新 `records/03-candidates/*.yaml` の ticker list を取得
2. 最新 `records/02-outlook/*.yaml` の `sectors` / `regions` を参照
3. candidates ticker のうち、所属業種/地域が `outlook` で `tailwind` または `neutral` のものを候補に残す（`headwind` は **除外**）
4. 候補から人間 + AI が個別 ticker を選定（詳細基準は [`components/research.md`](./components/research.md)）

#### 3.4.3 Path

```
records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
```

#### 3.4.4 Front matter

```yaml
---
ticker: "7203"
name: "..."
playbook: valuation-mean-reversion-v1 | valuation-catalyst-confirmation-v1
candidates_ref: records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml      # 必須
outlook_ref: records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml  # 必須
brief_refs:                                        # 任意
  - records/01-brief/YYYY/MM/YYYY-MM-DD-*.yaml
ai-draft: true | false
published_at: "ISO 8601"
tradable_at: "ISO 8601"
macro_gate: tailwind | neutral | headwind
valuation:
  per_forward: 数値 | null
  per_trailing: 数値
  pbr: 数値
  ev_ebitda: 数値
  p_s: 数値
  pcfr: 数値
  primary_metric: ["per_forward", "pbr"]
---
```

### 3.5 `records/05-trades/` — 執行記録

#### 3.5.1 責務

- research で採用された packet の entry / exit / position / P&L を記録

#### 3.5.2 Path

```
records/05-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
```

#### 3.5.3 Front matter

```yaml
---
ticker: "7203"
research_ref: records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md  # 必須
entry_date: "YYYY-MM-DD"
entry_price: 数値
position_size_pct: 数値
planned_exit:
  target_price: 数値 | null
  stop_loss: 数値
  time_stop_days: 40
status: open | closed
exit_date: "YYYY-MM-DD" | null
exit_price: 数値 | null
pnl_pct: 数値 | null
---
```

### 3.6 `records/06-reviews/` — 事後検証

#### 3.6.1 責務

- 決済後 +15 / +30 営業日レビュー + 月次 retro
- retro からの feedback を playbook / screening 改訂に繋ぐ

#### 3.6.2 Path

```
records/06-reviews/YYYY/MM/YYYY-MM-DD-<ticker>.md         # 個別 review
records/06-reviews/YYYY/retro-YYYYMM.md                   # 月次 retro
```

## 4. Front matter 共通原則

- 日時は ISO 8601 完全形（秒まで）、**quote 必須**: `"2026-04-24T09:00:00+09:00"`
- ticker は **4 文字の英数字文字列**として quote 必須（先頭 0 落ち防止、英字組入れ対応）: `"7203"`, `"130A"`
- null 許容フィールドは明示的に `null`
- AI 下書きは `ai-draft: true`、人間確認後 `false`
- 参照は path 配列: `brief_refs: [...]`, `updated_from: [...]`, `candidates_ref: ...`

## 5. ディレクトリ構造

```
baibai-loop/
├── README.md
├── docs/
├── records/
│   ├── _data/
│   ├── _ledger/
│   ├── _playbooks/
│   ├── _schemas/
│   ├── 01-brief/
│   ├── 02-outlook/
│   ├── 03-candidates/
│   ├── 04-research/
│   ├── 05-trades/
│   └── 06-reviews/
├── src/
├── tests/
├── .github/
├── pyproject.toml
└── uv.lock
```

## 6. 参考

- [`philosophy.md`](./philosophy.md): 思想・ベース概念
- [`design-principles.md`](./design-principles.md): 設計原則
- [`workflow.md`](./workflow.md): 日々の運用ワークフロー
- [`data-sources.md`](./data-sources.md): データソース
- [`components/`](./components/): 各成分の運用仕様
- [`screening/`](./screening/): スクリーニングサブシステム詳細
- [`templates/`](./templates/): 記入テンプレート
