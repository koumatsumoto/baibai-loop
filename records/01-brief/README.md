# records/01-brief/

4 成分アーキテクチャの **(a) マクロ事実ブリーフ** を蓄積するディレクトリ。世界情勢・日本経済・業種動向の一次情報を、短い事実 + 要点として時系列で記録する。

- **独立トラック**: 売買ループ（candidates → research → trade → review）から独立して積み上がる
- **アーキテクチャ上の位置付け**: `brief` を source とし、[`records/02-outlook/`](/docs/components/outlook.md) がマクロ見解を構築する

## 種類

| type | 頻度 | trigger | 典型例 |
| --- | --- | --- | --- |
| periodic | 日次 / 週次 / 月次 | 定期 | 世界 daily, 世界 weekly, 日本 monthly |
| event | 不定期 | 重大イベント（BOJ / FOMC / CPI 大振れ / 地政学 shock 等） | 会合決定当日のブリーフ |

**不定期 trigger 閾値**: 主要統計が予想対比 ±10% 以上乖離、金融政策変更、主要指数 ±3% 以上変動（詳細は [`docs/components/brief.md`](/docs/components/brief.md)）。

## 命名規則

```
records/01-brief/YYYY/MM/YYYY-MM-DD-{kind}-{slug}.yaml
```

- `{kind}` 例: `world-daily` / `world-weekly` / `macro-monthly` / `fomc` / `boj` / `cpi` / `gdp` / `geopolitics` / `event`
- `{slug}` は内容を端的に示す短い英小文字ハイフン区切り

例:

```
records/01-brief/2026/04/2026-04-19-world-weekly-us-iran-deescalation.yaml
records/01-brief/2026/04/2026-04-macro-monthly-us-cpi-hot.yaml
records/01-brief/2026/04/2026-04-30-fomc-hold.yaml
```

## YAML 必須項目

```yaml
schema_version: 1
kind: world-weekly | world-daily | macro-monthly | fomc | boj | cpi | gdp | geopolitics | event
type: periodic | event
scope: world | japan | sector-xx
ai_draft: true | false
published_at: "ISO 8601"
observation_date: "YYYY-MM-DD"
sources:
  - id: <id>
    name: <name>
    url: <URL>
    accessed_at: "YYYY-MM-DD"
    status: ok | partial | failed
layers:
  world: {...}
  japan: {...}
  japan_equity: {...}
next_events: [...]
```

詳細: [`docs/components/brief.md`](/docs/components/brief.md) と [`records/_schemas/brief-v1.json`](/records/_schemas/brief-v1.json)

## 運用ルール

- 思想・ベースの考え方: [`../docs/philosophy.md`](/docs/philosophy.md)
- アーキテクチャ正本: [`../docs/architecture.md`](/docs/architecture.md)
- 設計原則: [`../docs/design-principles.md`](/docs/design-principles.md)
- 日次 template: [`../docs/templates/brief-world-daily.yaml`](/docs/templates/brief-world-daily.yaml)
- 週次 template: [`../docs/templates/brief-world-weekly.yaml`](/docs/templates/brief-world-weekly.yaml)
- 月次 template: [`../docs/templates/brief-japan-monthly.yaml`](/docs/templates/brief-japan-monthly.yaml)
- 不定期 template: [`../docs/templates/brief-event.yaml`](/docs/templates/brief-event.yaml)
- 更新頻度・引用形式: [`../docs/workflow.md`](/docs/workflow.md)
- 作成前の欠損確認: [`../docs/workflow.md#brief-作成前の欠損確認`](/docs/workflow.md#brief-作成前の欠損確認)
- データソース: [`../docs/data-sources.md`](/docs/data-sources.md)

INDEX ファイルは設けない。一覧は `git ls-files records/01-brief/` または GitHub ツリーで確認する。
