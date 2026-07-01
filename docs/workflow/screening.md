---
title: "Workflow — screening"
summary: "割安 screening：全上場普通株を universe に、playbook-linked screen で割安ゾーンを機械抽出し candidates 事実を出力、select で research 候補を triage する。"
doc_type: workflow
status: active
last_reviewed: 2026-07-01
---

# Workflow — 割安 screening

単一ループ（[`../doctrine.md`](../doctrine.md) §2）の「お買い得を機械的に見つける」工程。全上場普通株を universe に、**valuation ranking で割安ゾーンを機械抽出**して `records/04-candidates/` の事実を出力し、`select` で深掘り候補を triage する。ここは決定論的な機械処理（L2）で、出力は解釈を含まない **事実**。指標算出の仕様は [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)、CLI / SQLite の実装は [`../reference/screening-runtime.md`](../reference/screening-runtime.md)、契約の正本は `records/_schemas/candidates.json`。

## Universe（対象範囲）

screen の評価対象（scope）は **全上場普通株** とし、規模・流動性・上場期間・規制 flag は除外条件ではなく **candidates に記録される事実** として扱う。

| 条件 | 値 | 理由 |
| --- | --- | --- |
| 銘柄種別 | 普通株のみ（ETF / REIT / 優先株を除外） | 事業会社の valuation 判定が対象 |
| 上場市場 | プライム / スタンダード / グロース | 一般投資家が取引可能 |
| bar 履歴 | 直近 20 営業日以上 | 売買代金・自己レンジ fact の算出に必要な最小データ |

research 候補への絞り込み（時価総額・売買代金・上場期間・JPX 規制）は **分析層のパラメータ `selection.liquidity`**（既定: 時価総額 100 億円以上・20 営業日平均売買代金 1 億円以上・上場 182 日以上・JPX 規制銘柄除外）として `select` 時に適用する。データを狭めない（どの銘柄も screening 事実を持つ）ことで、絞り込みを可視・可変にする。

**valuation 比較の母集団**（sector / 市場中央値）は `selection.liquidity` を満たす流動性母集団に固定し、小型・低流動性銘柄の混入で判定が歪まないようにする。

## 割安の判定軸

各銘柄について、[`../reference/valuation-metrics.md`](../reference/valuation-metrics.md) の指標（PER forward/trailing・PBR・EV/EBITDA・P/S・PCFR・OCF yield・FCF yield・net-cash ratio）を、**業種中央値相対** と **過去自己レンジ（直近 750 営業日 ≒ 3 年、上場 3 年未満は上場来）相対** の percentile として出す。スコアは軸別の座標であり、単一の合成点や売買指示には畳まない（[`../doctrine.md`](../doctrine.md) 柱 5）。

## Playbook-linked screen（OR 条件、最低 1 つ）

以下の割安 screen のうち **最低 1 つ** を満たす銘柄を通過とする。閾値は `records/_config/screening-rules/*.yaml` を正本とする。複数 hit は research 優先度を上げる材料。`evidence_hits[]` に playbook 名・hit reasons・判定 metrics を記録する。

- **`valuation-reversion`**：PER / PBR / 正の EV/EBITDA が業種中央値比・過去自己レンジ下位で割安。過去 60 営業日の下落で割安ゾーンへ入った銘柄を含む（長期保有の入口として押し目を拾う。短期 exit の signal ではない）。銀行・証券・保険・その他金融は除外（規制資本・与信サイクルで事業会社と同じ判定ができない）。
- **`cash-rich-asset-discount`**：CashEq / market cap・price-to-equity・equity ratio で cash-rich / 資産割安を拾う。EDINET の net-cash で contradiction を抑止。営業赤字・営業利益 YoY 急減（deterioration gate）は除外。金融・電気ガス・卸売・不動産は除外（BS の意味が異なる）。
- **`cashflow-yield-discount`**：期間正規化した CFO TTM の OCF yield で現金創出力の割安を拾う。CF 悪化（`cfo_yoy` 下限）・営業利益 YoY 急減・FCF マイナス（重設備）は除外。金融・電気ガスは除外。
- **`sales-discount-growth`**：P/S が業種中央値比で安く売上成長が残る銘柄。chronic loser を防ぐ operating margin floor を課す。金融は除外。

各 screen の金融等の除外根拠と deterioration gate の詳細は `records/_config/screening-rules/*.yaml` と本 doc を正本とする。

## Selection lens（triage）

`select` は candidates と最新 macro context を突き合わせ、深掘り候補を triage する。lens は割安ゾーンを狭めるのではなく、**耐性と重複** で着手順位を付ける。

| lens | 目的 | 扱い |
| --- | --- | --- |
| `durability`（塩漬け耐性） | 長期保有に耐えるか（net-cash・営業 CF 黒字・低負債・借換耐性・配当）を `high\|medium\|low\|unknown` で annotation | 採用の必須確認（[`../portfolio-management.md`](../portfolio-management.md) の耐性ゲート）へ接続する gate。ranking には使わない |
| `prior_research` | 過去 research で deferred / rejected とした候補の再登場を抑制し、同じ候補への偏りを下げる | `records/05-thesis/` の判断履歴から再構成する |

ranking の主キーは **valuation discount（割安度）** とする。Selection profile の built-in は `balanced` のみ。閾値変更は `records/_config/screening-rules/` の rules 設定を編集して `select` を再実行し output を diff する。**短期の急落を上位化する fast-dislocation boost と、リスクオン相場で逆張りを中立化する regime lens は持たない**（期間ではなく valuation と耐性で判断するため）。

## Candidates 出力（事実）

```text
records/04-candidates/YYYY/MM/YYYY-MM-DD.yaml
```

1 実行 = 1 ファイル（週次運用）。candidates は L1 SQLite からの決定論的な L2 出力であり、**git に積まない local store**（`.gitignore` 対象）としてローカル保持する。契約の正本は `records/_schemas/candidates.json`。核心 field は ticker / name / sector_33 / valuation 指標 / `metrics`（flat な派生値）/ `ttm_quality` / `evidence_hits[]`（通過 screen・hit reasons・判定 metrics）。`universe_size` に scope（全普通株 + bar 履歴）の銘柄数を記録し母集団確認に使う。欠損は `null` で明示し、単一総合 score は持たせない。

## 実行

```bash
uv run baibai-loop-screening bootstrap-cache --asof YYYY-MM-DD
uv run baibai-loop-screening extract-edinet-metrics --asof YYYY-MM-DD --lookback-days 540
uv run baibai-loop-screening verify-cache-coverage --asof YYYY-MM-DD
uv run baibai-loop-screening run --asof YYYY-MM-DD
uv run baibai-loop-screening select --asof YYYY-MM-DD --macro-context <path>
```

`run` は開始時に SQLite coverage を検証し、不足時は fail-fast（provider API へフォールバックしない）。JPX 規制情報と EDINET 前処理済み metrics は必須入力。`select` は macro context の `sector_tilts` を追い風/向かい風 lens として使い（hard gate にしない）、`recommendations` と `selection.diagnostics` を出す。手順・env・SQLite schema の実装詳細は [`../reference/screening-runtime.md`](../reference/screening-runtime.md)。

## 事実と分析の分離

candidates は **事実層**。閾値適用・screen hit は機械的で、「なぜ割安か」「採用すべきか」の解釈は [`./research.md`](./research.md) 側で行う（[`../doctrine.md#fact-analysis-separation`](../doctrine.md#fact-analysis-separation)）。candidates 本文に因果・予測・相場観を書かない。

## 参考

- [`../reference/valuation-metrics.md`](../reference/valuation-metrics.md)：指標算出仕様
- [`../reference/screening-runtime.md`](../reference/screening-runtime.md)：CLI / provider / SQLite schema
- [`./macro.md`](./macro.md)：select が使う sector_tilts
- [`./research.md`](./research.md)：candidates を起点にした個別調査
- [`./playbooks.md`](./playbooks.md)：割安 value の archetype
