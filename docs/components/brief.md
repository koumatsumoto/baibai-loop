# components/brief.md

Baibai-Loop 4 成分アーキテクチャの **(a) マクロ事実ブリーフ** の運用仕様。全体構造は [`../architecture.md`](../architecture.md) を参照。

## 1. 役割

- 世界情勢・日本経済・業種動向の **一次情報** を短く記録する
- 「事実 + 要点」の短いドキュメントで、解釈は入れない
- **独立トラック**: 売買ループ（candidates → research → trades → reviews）から独立に積み上がる
- Macro track の出発点として `records/02-outlook/` の source となる

## 2. 種類

### 2.1 periodic（定期）

| kind | 頻度 | 対象 | template |
| --- | --- | --- | --- |
| `world-daily` | 日次（営業日ベース、鮮度補完が必要な日） | 前日以降に増えた一次統計・会合日程・市場事実のうち、週次まで待つと outlook が stale になるもの | [`../templates/brief-world-daily.yaml`](../templates/brief-world-daily.yaml) |
| `world-weekly` | 週次 1 回 | 世界情勢・グローバル市場指標・地政学速報 | [`../templates/brief-world-weekly.yaml`](../templates/brief-world-weekly.yaml) |
| `macro-monthly` | 月次 1 回（主要発表の出揃い後） | CPI / 雇用統計 / 政策金利変更などの月次〜四半期統計 | [`../templates/brief-japan-monthly.yaml`](../templates/brief-japan-monthly.yaml) |

### 2.2 event（不定期）

| kind | trigger | template |
| --- | --- | --- |
| `fomc` | FOMC 会合当日または翌営業日 | [`../templates/brief-event.yaml`](../templates/brief-event.yaml) |
| `boj` | 日銀金融政策決定会合当日 | 同上 |
| `cpi` | CPI 発表日（予想対比 ±0.5% 以上乖離時） | 同上 |
| `gdp` | GDP 速報発表日 | 同上 |
| `geopolitics` | 地政学 shock（戦争・主要制裁・政権交代等） | 同上 |
| `event` | 上記に該当しない重大イベント | 同上 |

### 2.3 不定期 trigger 閾値

以下のいずれかを満たしたとき、event brief を作成する:

- 主要統計が予想対比 ±10% 以上乖離
- 金融政策変更（利上げ・利下げ、YCC 調整等）
- 主要指数 ±3% 以上変動（Nikkei 225, S&P 500, 米 10Y 利回り ±15bp 等）
- 地政学 shock（戦争勃発、主要制裁、政権交代、中央銀行総裁交代など）

閾値未満のイベントは次回の定期 brief（週次/月次）で扱う。

単一統計の通常公表日は、まず `world-daily` に記録する。`event` に昇格させるのは、上記閾値を満たす surprise、金融政策変更、地政学 shock のいずれかがある場合に限る。

## 3. Path と命名

```
records/01-brief/YYYY/MM/YYYY-MM-DD-{kind}-{slug}.yaml
```

- `{kind}`: `world-daily` / `world-weekly` / `macro-monthly` / `fomc` / `boj` / `cpi` / `gdp` / `geopolitics` / `event`
- `{slug}`: 内容を端的に示す短い英小文字ハイフン区切り
  - 事実ベース: 指標名と値を組み合わせた中立表現（例: `us-cpi-3p3`, `jp-core-cpi-sub2`, `us-10y-down`）
  - 解釈語（`beat`, `surge`, `rally`, `crash`, `hot`, `cool`）は避ける
  - 数値の小数点は `p` で代用（`3.3%` → `3p3`）
  - 目立つ事実がない観測月は `overview` を用いてよい
- INDEX ファイルは作らない。一覧は `git ls-files records/01-brief/` または GitHub ツリーで確認

## 4. YAML 必須項目

```yaml
schema_version: 1
kind: world-weekly | world-daily | macro-monthly | fomc | boj | cpi | gdp | geopolitics | event
type: periodic | event
scope: world | japan | sector-xx
ai_draft: true | false
published_at: "ISO 8601"       # 例: "2026-04-24T09:00:00+09:00"
observation_date: "YYYY-MM-DD"
period:                        # world-weekly では必須
  start: "YYYY-MM-DD"
  end: "YYYY-MM-DD"
  market_basis_date: "YYYY-MM-DD"
month: "YYYY-MM"               # macro-monthly では必須
references:
  prev_period: records/01-brief/.../...yaml | null
  latest_monthly: records/01-brief/.../...yaml | null
sources:
  - id: <id>
    name: <ソース名>
    url: <URL>
    accessed_at: "YYYY-MM-DD"
    status: ok | partial | failed
layers:
  world: {...}
  japan: {...}
  japan_equity: {...}
deltas:                        # world-weekly / macro-monthly では必須
  threshold_breaches: [...]
  unjudgeable: [...]
  direction_history: [...]
next_events: [...]
```

- `schema_version`: 現状は `1`
- `kind` / `type` / `scope`: 種別と範囲
- `ai_draft`: AI 下書き段階では `true`、人間確認後 `false`
- `published_at`: 作成/公開日時（ISO 8601 完全形、quote 必須）
- `observation_date`: 観測日（実作成日）
- `period`: 週次 brief の対象期間と市場データ基準日
- `month`: 月次 brief の対象月
- `sources[]`: 参照した一次統計を構造化記録。本文の各 indicator / event は `source_ids: [<id>, ...]` で sources を参照する
- `layers`: `world` / `japan` / `japan_equity` の 3 キー必須。該当なしは `{}` を明示
- `deltas`: 週次 / 月次は必須。閾値超え（`threshold_breaches`）、判定不能（`unjudgeable`）、方向履歴（`direction_history`）、方向反転（`direction_reversals`）を構造化
- 詳細な schema は [`../../records/_schemas/brief-v1.json`](../../records/_schemas/brief-v1.json)

## 5. 更新頻度とワークフロー

### 5.1 periodic

- **日次**: `world-daily` は「週次まで待つと stale になる事実」の受け皿。新しい一次統計、公表済み会合日程の更新、outlook に効く fresh fact が増えた営業日に作成する
- **週次**: 毎週 1 回、世界情勢・グローバル指標・地政学速報を `world-weekly` として記録。[`../workflow.md`](../workflow.md) の「brief の分析階層」節に従う
- **月次**: 毎月 1 回、主要統計の出揃いを待って `macro-monthly` として記録。差分データ（MoM / YoY）を計算

### 5.2 日次 brief の位置付け

- `world-daily` は **週次の縮小版ではない**。目的は、`records/02-outlook/` の入力に必要な鮮度を補うこと
- `macro-monthly` がまだ閉じていない月でも、当日公表された CPI / 小売売上高 / 雇用関連などの **月次級データを一時的に保持してよい**
- 後日 `macro-monthly` が作成されたら、その月次級データの正本は `macro-monthly` に移る。既存の `world-daily` は archive として保持し、以後の `world-daily` / `world-weekly` では再掲せずリンクで参照する
- bootstrap outlook の直前に最新 brief が古い場合は、`world-daily` または `event` を先に追加して freshness gap を埋める
- 同じ統計を `world-daily` と `event` の両方で重複生成しない。通常の月次級統計公表は `world-daily`、decisive event のみ `event`

### 5.3 event

- trigger 発生時に当営業日中に作成する（理想、遅くとも翌営業日まで）
- `sources` に必ず一次統計を含める
- 政策変更や閾値超え surprise を 1 件 1 brief で切り出す。routine な統計更新の受け皿にはしない

### 5.4 事実と分析の分離

- brief は **事実レイヤー専用**。解釈・予測・相場観を書かない
- 「〜を示唆する」「〜を受けて」「〜の背景に」などの因果推論表現を地の文に入れない
- 因果は報道引用として明示する場合のみ許容（`CNBC は中東情勢緩和を下落要因として挙げている`）
- 詳細: [`../workflow.md`](../workflow.md) の「事実記述の粒度」節

## 6. outlook への接続

- `records/02-outlook/` は複数の brief を積み上げて作成される（`updated_from` で brief ファイル path を列挙）
- brief 自体は outlook の存在を意識しない。brief は独立に積み上がる

## 7. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| 一次統計の数値取得・引用形式整備 | ○ | |
| 前週/前月比の計算 | ○ | |
| 方向履歴の生成 | ○ | |
| template への下書き埋め込み | ○ | |
| **一次ソース URL の有効性確認** | | ○ |
| **事実と解釈の混入チェック** | | ○ |
| 最終 commit | | ○ |

AI 下書きは `ai_draft: true` で識別し、人間確認後 `false` に更新する。

## 7.1 commit 前 self-review (anti-pattern との対応)

brief を書いた / 更新した後、commit 前に以下を必ず確認する。詳細チェックリストは
[`../anti-patterns.md`](../anti-patterns.md) を参照:

- [ ] **AP-01** (一次情報直接確認): すべての数値・固有名詞に一次情報 URL を紐付けたか。source の policy / rate / date / scenario が本文主張と一致しているか
- [ ] **AP-02** (数値検算): 前期比・前年比の計算結果を電卓 / Python で検算したか
- [ ] **AP-04** (schema 整合): `Indicator` には `note` 不可、`MonthlyStatistic` の `release_date` は `null` か非空文字列のみ等、`records/_schemas/brief-v1.json` を読み返したか
- [ ] **AP-05** (fact / 分析の境界): 「示唆」「受けて」「正当化材料」「early signal」「顕在化」「構造要因」「注目すべき」「重要な」等の解釈・因果推論・重要度評価表現が地の文に含まれていないか
- [ ] **AP-06** (source status と Tier の取り扱い): fact item の `source_ids` には少なくとも 1 つ `status: ok` の source を含めているか。`status: failed` の Tier 1 source だけで fact 値を入れていないか。Tier 1 が継続的に取れない指標は [`../data-sources.md`](../data-sources.md) §「一次統計の数値で Tier 1 取得が困難な場合の Tier 2 例外運用」に従って `failed` Tier 1 + `ok` Tier 2 を併記しているか
- [ ] **AP-07** (公表日確認): 各 monthly_statistic / event の `release_date` を一次 source の発表日と照合したか。発行日 ± 5 営業日に予定された FOMC / BOJ / OPEC+ / CPI / PCE / NFP の最新 release が出ていれば必ず取り込んだか

## 8. 参考

- [`../philosophy.md`](../philosophy.md): 思想（事実と分析の分離、マクロ優位）
- [`../architecture.md`](../architecture.md): 全体構造
- [`../workflow.md`](../workflow.md): brief の詳細運用ルール（閾値、差分、禁止表現）
- [`../data-sources.md`](../data-sources.md): 一次統計ソース Tier
- [`../templates/brief-world-daily.yaml`](../templates/brief-world-daily.yaml): 日次 template
- [`../templates/brief-world-weekly.yaml`](../templates/brief-world-weekly.yaml): 週次 template
- [`../templates/brief-japan-monthly.yaml`](../templates/brief-japan-monthly.yaml): 月次 template
- [`../templates/brief-event.yaml`](../templates/brief-event.yaml): 不定期 template
