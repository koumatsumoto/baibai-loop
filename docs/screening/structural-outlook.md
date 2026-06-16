---
title: "Structural outlook and candidate scorecard"
summary: "AI / structural-decline annotation taxonomy and the multi-axis L3 triage scorecard over screen output."
doc_type: reference
status: active
last_reviewed: 2026-06-15
---

# Structural outlook and candidate scorecard

この doc は、screen output に対する 2 つの分析層 (L3) 補助の正本仕様です。どちらも事実層の candidates と forward 計測済みの `select` ranking を変えません。

## 1. Structural outlook annotation

`structural_outlook` は候補事業の長期構造性を `ai_tailwind | neutral | structural_decline` で示す annotation です。research が long-hold quality を AI 構造性で評価し、構造的に縮小する需要を避けるための補助に使います。

- **screen gate ではない**: candidates の通過可否を変えない。
- **ranking sort-key ではない**: `select` の推奨順位を変えない。AI 期待を ranking に畳み込むと forward 計測の規律と「AI を単独の採用 / sizing / ranking / validator rule にしない」portfolio policy ([`../portfolio-policy.md`](../portfolio-policy.md)) に反するため。
- **validator rule ではない**: records の検証対象にしない。

### 1.1 分類の優先順位

最初に一致したものを採用します。

1. `ticker_overrides` — ticker 単位の個別判断 (最優先)。sector default では拾えない例外を上書きする (例: 業種「機械」の遊技機メーカーを `structural_decline` に、業種外の半導体商社を `ai_tailwind` に)。
2. `sector_defaults` — 東証 33 業種の既定 tilt。
3. いずれも無ければ `neutral`。

### 1.2 Config

taxonomy の正本は `records/_config/structural-outlook/<effective_from>.yaml` です。`sector_defaults`(業種名→outlook) と `ticker_overlooks`(ticker→{outlook, note}) を持ちます。閾値ではなく人手で維持する curated reference config で、編集にコード変更は要りません。各分類は「なぜその outlook か」を `note` / コメントに現在形で書きます。

### 1.3 `select` への出力

`select` / `select-sweep` は config が読めるとき (既定で committed config を graceful 読み込み) `lenses.structural_outlook = {outlook, basis, note}` を各候補に付け、`diagnostics.structural_outlook_counts` に件数を出します。`reason_tags` に `ai_tailwind`、`risk_tags` に `structural_decline` を反映します。config が無い checkout では annotation を出さずに動作します。

## 2. Candidate scorecard

`scorecard` は週次 screen output を、流動性と構造 tilt で絞った shortlist にし、各候補の risk-reward を**軸別座標**で出す L3 triage です。

- 軸 (座標。単一合成スコアにしない — [`../design-principles.md`](../design-principles.md) §9): `valuation_discount` / `cashflow_durability` / `balance_sheet` / `dislocation` / `long_hold` / `structural`。
- 表示順は lexicographic な triage (structural tilt → long-hold rating → lane order → evidence strength) で、既存の ranking primitive を再利用する。これは長い shortlist を扱うための便宜であり、forward 計測した ranking ではない。採用判断は research と人間に残す。
- 機械的 screen と `select` の推奨 queue は変更しない。

### 2.1 使い方

```bash
uv run baibai-loop-screening scorecard \
  --asof YYYY-MM-DD \
  --top 12 \
  --include-outlook ai_tailwind \
  --exclude-ticker 1234,5678
```

- `--include-outlook` (繰り返し可): 残す outlook。既定は `ai_tailwind` と `neutral` (= `structural_decline` を落とす)。AI 傾斜で絞るときは `ai_tailwind` のみ。
- `--exclude-ticker`: 既存保有など除外する ticker (comma 区切り)。
- `--structural-config`: taxonomy config の override (既定は committed config)。

出力は `scorecard` (counts / 除外内訳 / by_outlook / by_sector / display_order_note) と `shortlist` (各候補の axes) を正本にします。

## 上位 docs

- 機械的ふるいと selection lens 境界: [`mechanical.md`](./mechanical.md) §3.8
- 設計原則 (軸別座標、単一スコア非採用): [`../design-principles.md`](../design-principles.md)
- portfolio policy (AI を単独 rule にしない): [`../portfolio-policy.md`](../portfolio-policy.md)
