# components/macro-context.md

Macro context は、スクリーニング前に読む単一の macro analysis artifact です。外部記事・指標 series・AI/人間の判断をここに集約します。

## 目的

- ビジネス価値は「お買い得銘柄を拾う」ことです。Macro context は実装対象ではなく、候補選定前に市場環境と sector bias を確認するための入力です。
- 記事本文や監査ログは保存しません。必要な URL、タイトル、使った理由だけを `inputs` に残します。
- Macro context は hard gate ではありません。特定 sector が headwind でも候補を自動除外せず、research で確認すべき観点として出します。

## 保存場所

```text
records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml
```

Template: [`../templates/macro-context.yaml`](../templates/macro-context.yaml)

## 作成頻度

Macro context は種類を増やしません。通常はスクリーニング手前で最新 context を確認し、以下に該当すれば更新します。

- FOMC、BOJ、CPI、雇用統計など主要 macro event 後
- 為替、金利、原油、日経平均などが screening 判断を変える程度に動いたとき
- 候補が特定 sector に偏り、追加の macro 前提確認が必要になったとき

## Field

- `kind`: `macro-context`
- `context_id`: `macro-context-YYYY-MM-DD-<slug>`
- `as_of` / `valid_until` / `published_at`: 有効期間と作成時点
- `inputs.articles`: 外部記事の source / title / url / used_for
- `inputs.indicator_series`: `baibai-loop-macro` 等で確認した series と window
- `sector_tilts.items`: `sector_33` exact match で使う sector tilt
- `research_questions`: 個別銘柄 research で確認する問い
- `refresh_triggers`: 次に更新すべき条件
- `changes_since_previous`: 前回からの主な変化

## CLI

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD --macro-context records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-slug.yaml
uv run baibai-loop-validation --target macro-context
```
