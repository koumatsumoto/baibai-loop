---
name: ai-value-bargain-selection
description: >-
  長期的に AI で企業価値が高まる × 今のトレード状態で割安になっている日本株を、
  スクリーニング基盤から TOP12 → 一次 IR 深掘り → 4 銘柄 → 最良リスクリワード 1 銘柄へ
  絞り込み、HTML レポートと GitHub Issue で提案するまでの end-to-end 手順。
  「お買い得な銘柄を選定して」「新規に買う AI 割安株を選んで」「今買う銘柄を提案して」
  と言われたとき、または同種の銘柄選定を再現するときに使う。
---

# AI バリュー・バーゲン銘柄選定（Baibai-Loop）

長期 AI 構造価値 × 足元割安の日本株を、本リポジトリの screening 基盤で選定し提案する手順。`AGENTS.md` の anti-pattern（AP-01 一次情報 / AP-02 検算 / AP-09 会社 IR 確認）、`docs/portfolio-policy.md`（swing-first / long-hold-capable + AI long-term structural impact）、`docs/design-principles.md`（単一合成スコアを出さない＝スコアは軸別座標）に従う。

## 0. ゴールと前提

- **ゴール**: 長期的に企業価値が高まる銘柄のうち最もお買い得なものを選定し、ユーザーに提案する。最終提案はユーザーがレビューして決める（research memo の `approved` は決定後に作る）。
- **前提の固定**: (a) AI の解釈の広さ（本命AIのみ / AI受益まで広く / RR最優先で範囲不問）、(b) 投資フレーム（swing-value / 長期コンパウンダー / ブレンド）をユーザーに確認する。曖昧なら `AskUserQuestion` で 2 問だけ確認して着手する。
- **除外**: 既存保有銘柄（`records/06-trades/` から）は「新規」候補から外す。パチンコ機械のような構造的に廃れる事業は人間判断で外す。

## 1. 準備

```bash
# 現保有（新規候補から除外する ticker）
ls records/06-trades/*/*/*.md
# データ鮮度（最新営業日 = screen の asof）
python3 -c "import sqlite3;c=sqlite3.connect('data/screening/market.sqlite');print(c.execute('SELECT MAX(traded_at) FROM jquants_daily_bars').fetchone())"
# 最新 candidates（無ければ docs/operations/screening-runbook.md で生成）
ls records/04-candidates/*/*/*.yaml | tail -3
```

## 2. macro context を「深く」作る（リスクリワードの土台）

[[feedback_macro_context_depth]]: 浅い macro は不可。直近の世界情勢を入念に多角的に分析し、RR を判断できる前提にする。トークンは気にせず最大限。

- **テーマ別 subagent fan-out**（[[feedback_subagent_cap]] により**同時起動は最大 5**。多ければ wave に分ける）:
  1. AI / 半導体 / データセンター capex サイクル（拡大 or digestion かが AI-tilt の RR を左右）
  2. 米マクロ・Fed・金利・米株（Mag7 集中度含む）
  3. 日本マクロ・BOJ・JGB・USD/JPY・春闘・需給/PBR 改革
  4. 地政学・通商・半導体規制・台湾・原油
  5. クロスアセット・シナリオ（base/bull/bear/tail）・直近急落の post-mortem・invalidation
- 各 agent に: Tier-1 を多数（20+ 目安、各 URL+公表日）、確認/推定を区別、**「割安な日本 AI/DX 株を今買う RR にどう効くか」へ接続**、を要求。session 制限に備え「partial でも必ず結論を返す」と指示。
- 合成して `records/01-macro-context/YYYY/MM/macro-context-YYYY-MM-DD-<slug>.yaml` を作る。schema 必須: `kind, context_id, as_of, valid_until, published_at, summary, inputs(articles[]+stats_series[]), sector_tilts.items[](id/scope=sector_33/key/stance∈tailwind|neutral|mixed|headwind/strength/confidence/rationale), research_questions[], refresh_triggers[], changes_since_previous[]`（additionalProperties=false）。`uv run baibai-loop-validate --target macro-context` を通す。
- `select` は macro `as_of` が candidates asof より新しいと拒否する。mechanical run には asof 以前で valid な context を使い、買い判断の深い分析は最新 context で行う。

## 3. `select` で割安候補 TOP10 を出して人手で TOP12 を確定する

screening の正本 ranking (`select`) を最新 macro context に対して走らせ、軸別座標 (lane / lenses / market_regime / 流動性除外件数) を含む診断付き payload を取得する。AI 構造性は §4 の一次 IR 深掘りで人間判定する (scorecard / structural-outlook 系のサブシステムは前回 cleanup で削除済み)。

```bash
uv run baibai-loop-screening select --asof YYYY-MM-DD --top 10 --detail full > .cache/select-<asof>.yaml
```

- 出力 `recommendations[]` から **既存保有 ticker** と **構造衰退業種 (パチンコ機械 / 旧来繊維機械 / 印刷等)** を skill 側 post-filter で除外し、TOP12 候補を確定する (`select` には除外フラグはない)。
- `select` は valuation-reversion / cash-rich-asset-discount / cashflow-yield-discount / sales-discount-growth の 4 lane を `evidence_hits` で示し、`selection.diagnostics.market_regime` で benchmark trend (risk_on_rally / neutral_range / risk_off_selloff) を返す。表示順は forward-measured ranking で verdict ではない。
- 候補に厚みが必要なら `--top 20` まで広げて post-filter 後に 12 件を確保する。

## 4. TOP12 を一次 IR 深掘り（≤5 subagent / wave）

- **同時 5 まで**。12 銘柄なら 2-3 銘柄/agent × wave で回す。各 agent に `km:ir-research` の規律（一次/準一次 2 ソース検算、決算期・分割の取り違え厳禁、見出しの罠回避）を要求。
- 各銘柄で出す: 事業/売上構成、**AI/DX 構造性の本物度（具体的製品で懐疑的に。後付けを見抜く）**、長期見通し、直近通期+来期予想、valuation、**なぜ今割安/下落したか（決算ミス / ガイダンス減 / 需給 de-rating / 全体安の切り分け）**、財務/下値（net cash・営業CF・自己資本比率）、株主還元、リスク/invalidation、総合判定（AI 長期価値 × 割安度 × RR）。出典 URL と確度を必須に。
- 返ってきた数値は candidate row（2026-06-12 等）や EDINET 値と相互検算する。

## 5. 4 銘柄に絞り、最良リスクリワード 1 銘柄を選ぶ

- 軸で横並び比較（単一合成スコアに畳まない）。重視: **AI 構造性の確度（後付けでない）× 割安度（de-rating であって業績崩壊でない）× 下値保護（net cash/CF/還元）× 近接 catalyst × swing 妙味 / 長期保有の質**。
- 「割安に理由がある」value trap（減配・減益ガイド・規制 overhang・のれん減損リスク）は減点。AI ラベルが最弱セグメントに偏在する銘柄は本物度を下げる。
- 4 銘柄 + 最良 1 銘柄を確定し、各々に entry/target/stop/invalidation と policy 準拠の sizing（`src/baibai_loop/policy_config.py`: tactical budget・1注文上限・8% ticker cap・board lot 100）を付す。`expected_upside=(target/entry-1)*100`、`expected_downside=(1-stop/entry)*100`、`RR=upside/downside`（AP-02 で検算）。

## 6. HTML レポート + GitHub Issue で報告

- **HTML レポート**: `km:html-document` で 1 枚物の HTML を作る（内容＝市場 context の深い分析・4 シナリオ・主要リスク・select 軸別座標・4 候補比較・最良 RR の根拠・一次ソース。skill はレイアウト/セキュリティのみ担当）。リポジトリ内（例 `reports/`）に保存して commit する。
- **GitHub Issue**: 候補/プラン issue を作り、4 候補・最良 1・entry/exit/invalidation・sizing・主要リスクを記載し、**commit 済み HTML レポートのパス/リンクを添付**する。`km:github-workflow` に従う。

## 7. 検証・PR

基盤コードや records を触ったら commit 前に通す:

```bash
uv run baibai-loop-validate && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
```

1 issue = 1 PR、commit で分ける（[[feedback_pr_splitting]]）。基盤変更と選定成果物・docs を同一 PR に積む。

## 8. 完了条件

- AI 構造性が本物で、足元 de-rating で割安、下値保護のある銘柄を、一次 IR 出典つきで 4 つに絞り、最良 RR 1 つを根拠つきで選んだ。
- 深い macro context（多角・Tier-1 多数・シナリオ・リスク）が RR の前提として揃い、validate を通った。
- HTML レポートと GitHub Issue でユーザーがレビューできる形になっている。検証（validate/ruff/mypy/pytest）が緑。

## 9. 制約（必ず守る）

- **サブエージェント同時起動は最大 5**（[[feedback_subagent_cap]]）。多数対象は wave 化。
- **AI 期待を単独の採用 / sizing / macro fit / validator rule / ranking sort-key にしない**（`docs/portfolio-policy.md`）。AI 構造性は §4 の一次 IR 深掘りで人間判定する。
- **単一の合成スコア・売買指示を出さない**。スコアは軸別座標（`docs/design-principles.md`）。
- 既存保有と構造衰退（パチンコ機械等）は新規候補から外す。
- 最終採用判断はユーザー。提案は Issue + HTML レポートで渡し、`approved` research memo は決定後に作る。
