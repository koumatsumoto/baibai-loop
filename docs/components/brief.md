# components/brief.md

Baibai-Loop 4 成分アーキテクチャの **(a) マクロ事実ブリーフ** の運用仕様。全体構造は [`../architecture-v1.md`](../architecture-v1.md) を参照。

## 1. 役割

- 世界情勢・日本経済・業種動向の **一次情報** を短く記録する
- 「事実 + 要点」の短いドキュメントで、解釈は入れない
- **独立トラック**: 売買ループ（screened → research → trades → reviews）から独立に積み上がる
- Macro track の出発点として `view/` の source となる

## 2. 種類

### 2.1 periodic（定期）

| kind | 頻度 | 対象 | template |
| --- | --- | --- | --- |
| `world-weekly` | 週次 1 回 | 世界情勢・グローバル市場指標・地政学速報 | [`../templates/brief-world-weekly.md`](../templates/brief-world-weekly.md) |
| `macro-monthly` | 月次 1 回（主要発表の出揃い後） | CPI / 雇用統計 / 政策金利変更などの月次〜四半期統計 | [`../templates/brief-japan-monthly.md`](../templates/brief-japan-monthly.md) |

### 2.2 event（不定期）

| kind | trigger | template |
| --- | --- | --- |
| `fomc` | FOMC 会合当日または翌営業日 | [`../templates/brief-event.md`](../templates/brief-event.md) |
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

## 3. Path と命名

```
brief/YYYY/MM/YYYY-MM-DD-{kind}-{slug}.md
```

- `{kind}`: `world-weekly` / `macro-monthly` / `fomc` / `boj` / `cpi` / `gdp` / `geopolitics` / `event`
- `{slug}`: 内容を端的に示す短い英小文字ハイフン区切り
  - 事実ベース: 指標名と値を組み合わせた中立表現（例: `us-cpi-3p3`, `jp-core-cpi-sub2`, `us-10y-down`）
  - 解釈語（`beat`, `surge`, `rally`, `crash`, `hot`, `cool`）は避ける
  - 数値の小数点は `p` で代用（`3.3%` → `3p3`）
  - 目立つ事実がない観測月は `overview` を用いてよい
- INDEX ファイルは作らない。一覧は `git ls-files brief/` または GitHub ツリーで確認

## 4. Front matter 必須項目

```yaml
---
type: periodic | event
scope: world | japan | sector-xx
published_at: "ISO 8601"       # 例: "2026-04-24T09:00:00+09:00"
sources:
  - "URL or path"
---
```

- `type`: `periodic` か `event` のどちらか
- `scope`: 対象範囲（世界 / 日本 / 特定セクター）
- `published_at`: 作成/公開日時（ISO 8601 完全形、quote 必須）
- `sources`: 参照した一次統計 URL / path の配列

## 5. 更新頻度とワークフロー

### 5.1 periodic

- **週次**: 毎週 1 回、世界情勢・グローバル指標・地政学速報を `world-weekly` として記録。[`../workflow.md`](../workflow.md) の「brief の分析階層」節に従う
- **月次**: 毎月 1 回、主要統計の出揃いを待って `macro-monthly` として記録。差分データ（MoM / YoY）を計算

### 5.2 event

- trigger 発生時に当営業日中に作成する（理想、遅くとも翌営業日まで）
- `sources` に必ず一次統計を含める

### 5.3 事実と分析の分離

- brief は **事実レイヤー専用**。解釈・予測・相場観を書かない
- 「〜を示唆する」「〜を受けて」「〜の背景に」などの因果推論表現を地の文に入れない
- 因果は報道引用として明示する場合のみ許容（`CNBC は中東情勢緩和を下落要因として挙げている`）
- 詳細: [`../workflow.md`](../workflow.md) の「事実記述の粒度」節

## 6. view への接続

- `view/` は複数の brief を積み上げて作成される（`updated_from` で brief ファイル path を列挙）
- brief 自体は view の存在を意識しない。brief は独立に積み上がる

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

AI 下書きは `ai-draft: true` で識別し、人間確認後 `false` に更新する運用は screening 側 packet と整合させる。brief は front matter に `ai-draft` を入れない（全て人間最終確認前提）。

## 8. 参考

- [`../philosophy.md`](../philosophy.md): 思想（事実と分析の分離、マクロ優位）
- [`../architecture-v1.md`](../architecture-v1.md): 全体構造
- [`../workflow.md`](../workflow.md): brief の詳細運用ルール（閾値、差分、禁止表現）
- [`../data-sources.md`](../data-sources.md): 一次統計ソース Tier
- [`../templates/brief-world-weekly.md`](../templates/brief-world-weekly.md): 週次 template
- [`../templates/brief-japan-monthly.md`](../templates/brief-japan-monthly.md): 月次 template
- [`../templates/brief-event.md`](../templates/brief-event.md): 不定期 template
