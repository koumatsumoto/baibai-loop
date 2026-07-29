# Repository Agent Instructions

Baibai-Loop の運用作業を AI エージェントに任せるときの最小規約。リポジトリ全体の構造は [`README.md`](./README.md)、docs 入口は [`docs/README.md`](./docs/README.md) を読む。

## 作業前に必ず読む

- 思想・大戦略・語彙: [`docs/doctrine.md`](./docs/doctrine.md)
- 構造・repository map・CLI/SQLite 契約: [`docs/architecture.md`](./docs/architecture.md)
- 資本・ポジション管理: [`docs/portfolio-management.md`](./docs/portfolio-management.md)
- 各工程の手順: [`docs/workflow/README.md`](./docs/workflow/README.md)
- data sources / write-time validation / Python 基盤: [`docs/reference/README.md`](./docs/reference/README.md)
- **失敗パターンと再発防止**: [`docs/anti-patterns.md`](./docs/anti-patterns.md) — 過去の PR レビューで繰り返し指摘された類型集。macro context / research / write-time validation を編集する前に該当節のチェックリストを 1 周すること

## 運用の 2 サイクル（作業の入口）

日常の作業はほぼ次の 2 サイクルのどちらかに属する。まずどちらのサイクルの作業かを特定し、対応する runbook を正本として進める。

| サイクル | 内容 | 正本 runbook | skill |
| --- | --- | --- | --- |
| **継続的な投資判断** | 随時の機会判断 / 人間からの注文結果 / 月次入金 / 決算・重要event / 年次outcomeをtriggerごとに進める | [`docs/operations/decision-cycle.md`](./docs/operations/decision-cycle.md) | `decision-cycle`（必要時に`macro-analysis`） |
| **基盤改善** | 現状計測 → 仮説の事前登録 → design/confirm 検証 → 採用実装 → 運用テスト → dated report → 継続監視 | [`docs/operations/improvement-loop.md`](./docs/operations/improvement-loop.md) | `improvement-loop` |

## 効果と複雑性の均衡

subsystem、public CLI、schema、persistence、dependency、state、運用手順などの複雑性を増やす前に、期待効果の大きさと導入・保守・撤回コストを比較する。期待効果は[`doctrine.md#improvement-value-hierarchy`](./docs/doctrine.md#improvement-value-hierarchy)の価値階層を第一基準とし、利用頻度、evidence強度とあわせて評価する。作れることや実装済みであること自体を採用理由にしない。

ここでのコストに実装量・工数は含まない。規模と複雑性の扱いは[`doctrine.md#development-investment-policy`](./docs/doctrine.md#development-investment-policy)の開発投資の大方針を正本とする。

改善提案とreview指摘は、冒頭に`価値tier: Tn — <直接的な成果への因果経路>`を1行で宣言する。tierの定義、複数効果の扱い、T4の採用条件はdoctrineを正本とし、同じ定義をこの文書へ複写しない。

効果に見合う最小で可逆なsurfaceを選ぶ。初期サンプルや単発用途は`tools/`、既存output、operation sessionから始め、反復利用と効果を確認してからstable CLI、model、subsystemへ昇格する。将来の利用を仮定した未使用拡張、汎用化、永続stateは持ち込まない。

実装後のreviewでも効果対複雑性を再判定する。釣り合わない場合は一般化を削る、surfaceを縮小する、またはnon-adoptionとする。correctnessとsafetyに必要な検証・防御は「複雑だから」という理由で削らず、効果核を守る最小構成へ置く。

## サブシステム索引

サブシステム名（macro / screening / research / position など）を指定されたら、この表で src / store / CLI / 品質改善計器を引いて着手する。各工程の詳細は [`docs/workflow/`](./docs/workflow/)、依存構造は [`docs/architecture.md#repository-map`](./docs/architecture.md#repository-map) を正本とする。

| subsystem | src | store | CLI | 品質改善計器 |
| --- | --- | --- | --- | --- |
| macro | `src/baibai_engine/macro/` | `data/app/baibai.sqlite`（context）+ `data/indicators/macro.sqlite`（series） | `baibai-engine macro` | 見積り calibration（[`workflow/macro.md`](./docs/workflow/macro.md)、formal loop にしない） |
| screening | `src/baibai_engine/screening/` | `data/screening/runs.sqlite`（machine）+ `data/app/baibai.sqlite`（shortlist）+ `method/` | `baibai-engine screening` | 見積り calibration（保有 outcome + 長期 horizon の較正リプレイ `calibration-build/evaluate`。短期 backtest はしない） |
| research | `src/baibai_engine/research/` | `data/app/baibai.sqlite` + `method/playbooks/` | `baibai-engine research` / `baibai-engine research evaluate` | thesis + planning-only limit + holding-review composition |
| position | `src/baibai_engine/position/` | `data/app/baibai.sqlite` | `baibai-engine position` (`ledger` / draft / `apply-draft` / `outcome`) | human-confirmed portfolio ledger + holding review + portfolio outcome |
| operation / proposal | `src/baibai_engine/operation/`, `src/baibai_engine/proposals/` | `data/app/baibai.sqlite` | `baibai-engine operation` / `baibai-engine proposal` | current workspace + immutable final result / trade decision current state |
| market | `src/baibai_engine/market/` | （`data/screening/market.sqlite` ほか、git 外） | — | 価格・calendar data 層（screening・保有計測の価格基盤） |
| foundation | `src/baibai_engine/foundation/` | — | — | 共有 primitive（import sink、固有の計器なし） |
| task | `src/baibai_engine/tasks/` | `data/app/baibai.sqlite` | `baibai-engine task` | current task state |
| app | `src/baibai_app/` | application DBほかdomain storeをread-only合成 | `baibai-app` | Baibai Appのread model / local API |

品質改善は単一の見積り calibration に集約する: entry 時の見積り（RR・期待利回り・FV）を保有の実現結果と突き合わせ、加えて全銘柄の長期 horizon 較正リプレイ（[`docs/reference/estimate-calibration.md`](./docs/reference/estimate-calibration.md)）で見積り手法そのものを較正して、macro 読み・screening 閾値・FV 推定・耐性判定を離散的に改善する（短期 horizon の screen 成績最適化はしない。doctrine 柱 5）。これは日常の判断triggerとは独立した基盤改善である。詳細は各 [`docs/workflow/`](./docs/workflow/) doc を正本とする。

## Repository-local skills

repository-local skillの正本は`.agents/skills/<name>/SKILL.md`である。該当taskでは次表からskillを選び、SKILL.mdを全文読んでから操作する。`.claude/skills/<name>`は同じdirectoryへのrelative symlinkであり、別内容として編集しない。skillが参照するrunbook/referenceとpublic `--help`を優先し、tests/fixturesやsrcから日常手順を推測しない。

| task | skill |
| --- | --- |
| 候補抽出、IR、購入・指値提案、人間からの注文結果、保有review、年次outcome | [`.agents/skills/decision-cycle/SKILL.md`](./.agents/skills/decision-cycle/SKILL.md) |
| 判断のための市場環境評価レポート（macro context）を書く。指標の変化を確認したいだけなら`macro reading`を読み、レポートは作らない | [`.agents/skills/macro-analysis/SKILL.md`](./.agents/skills/macro-analysis/SKILL.md) |
| screening/FV/E[r]等の方法改善 | [`.agents/skills/improvement-loop/SKILL.md`](./.agents/skills/improvement-loop/SKILL.md) |

## 言語運用

人間向けの運用記録、調査メモ、作業メモ、最終報告は原則日本語で書く。ただし、schema field、ticker、tool output、コード/API 名、固有の英語指標名は自然に英語のままでよい。

## commit 前 / PR 前の self-review

method / src / docs の変更を含む commit を作る前に、[`docs/anti-patterns.md`](./docs/anti-patterns.md) の対応する anti-pattern (AP-01〜AP-10) のチェックリストを通過させること。特に以下は 100% 防ぐ:

- 一次情報を直接確認せず二次情報・推測で書く (AP-01)
- 数値計算を機械的に検算しない (AP-02)
- 株価異常値の corporate action 確認を skip する (AP-03)
- schema / 実装の意味を読まずに推測で解釈する (AP-04)
- macro context の根拠 URL / series / used_for を曖昧にする
- 公表日 / source の最新性確認を skip する (AP-07)
- validator の抜け道を意識しない (AP-08)
- 外部 AI 分析や system output を事実として thesis（application DB）に取り込む / canonical ledgerのcurrent + reserved exposureを再計算しない / 注文と約定の状態を区別しない (AP-09)

成分別の詳細チェックリスト:
- macro context 編集時: [`docs/workflow/macro.md`](./docs/workflow/macro.md)
- research 編集時: [`docs/workflow/research.md`](./docs/workflow/research.md)

メタ運用 (失敗パターンの再発防止):
- 同じ failure mode を 2 回以上 PR review で指摘されたら、[`docs/anti-patterns.md`](./docs/anti-patterns.md) の該当節を強化する
- 新しい write-time validation rule を追加するときは、anti-patterns.md AP-08 のチェックリストを必ず更新して次回 review で同じ穴が再発しないように記録する
- 一次情報 (Tier 1) が継続的に取得困難な指標は [`docs/reference/data-sources.md`](./docs/reference/data-sources.md) §「一次統計の数値で Tier 1 取得が困難な場合の Tier 2 例外運用」に従い、`status: failed` Tier 1 と `status: ok` Tier 2 を併記する
- Python 構文を review で指摘する前に、必ず [`pyproject.toml`](./pyproject.toml) の `requires-python` / Ruff `target-version` と [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) §3 を確認する。この repo は Python 3.14 固定だが、Ruff は `target-version = "py313"` にして PEP 758 の `except T1, T2:` へ自動整形されないようにしている。複数例外捕捉は必ず `except (T1, T2):` と書く

## 事実と分析の分離

screening run storeはobserved / derived / estimateを区別する機械出力層、application DB のmacro context・shortlist・thesisはjudgment層。candidatesにAI解釈・因果・相場観を書かず、E[r] / FV anchorを事実と呼ばない。詳細は[`docs/doctrine.md#fact-analysis-separation`](./docs/doctrine.md#fact-analysis-separation)。

## Codex sandbox routing

Codex の managed sandbox で実行不能と分かっている操作は、sandbox 内で試してから再実行せず、初回から承認経路へ送る。

- `gh`、`git fetch/pull/push` などの network 操作と、branch / stage / commit など `.git` への書き込み
- local socket / browser を使う `baibai-app serve`、headless Chrome、FastAPI `TestClient` を含む `pytest`
- `uv` が sandbox 外の cache へ書く操作。既存環境で足りる検証は `.venv/bin/{ruff,mypy,pytest,lint-imports}` を優先し、`uv` 自体が必要なら承認経路を使う
- Markdown を含む `gh issue/pr` の本文は `--body-file` で渡し、backtick や `$()` を shell の二重引用符へ埋め込まない

## 検証

application data のmodel / write pathを変更したら、コミット前に最低限以下を通す。

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run lint-imports
```

macro subsystem（`src/baibai_engine/macro/`・`method/macro-*`・indicator registry）に触れた変更では、
加えて次を通す。git 管理外の 2 store を突き合わせる検査であり、CI には application store が無いので
機械化できるのはここだけである。

```bash
uv run python tools/validate_macro_stores.py
```

これはローカル用の subset。drift gate・bandit・pip-audit・UI build を含む完全な CI gate は [`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md) §9 を正本とする。
