# Baibai-Loop

Baibai-Loopは、一人で日本株を長期運用するための意思決定基盤です。AIが市場観測、割安候補抽出、一次情報確認、3年・5年評価、独立反証、指値・数量提案、保有見直しを行い、人間が最終判断とbroker発注を行います。自動売買システムではありません。

成功は注文数や予算消化では測りません。永久的な資本毀損を抑え、その時点で最もお買い得な候補を納得可能な根拠とともに判断し、税・費用込みの長期総合returnを配当込みTOPIXと比較して、3年・5年単位で見積り能力を改善できることを成果とします。

## Start here

| 目的 | 入口 |
| --- | --- |
| 候補選定、指値、人間からの注文結果、保有review | [`docs/operations/decision-cycle.md`](./docs/operations/decision-cycle.md) |
| screening、FV、E[r]等の方法改善 | [`docs/operations/improvement-loop.md`](./docs/operations/improvement-loop.md) |
| 思想、優先順位、語彙 | [`docs/doctrine.md`](./docs/doctrine.md) |
| package、CLI、method、store | [`docs/architecture.md`](./docs/architecture.md) |
| AIへ作業させる | [`AGENTS.md`](./AGENTS.md)から`.agents/skills`を選ぶ |
| docs全体から探す | [`docs/README.md`](./docs/README.md) |

## What success means

候補は次の順で比較します。

1. 永久的資本毀損リスク
2. 5年期待総合returnとFV乖離
3. repository portfolioへの追加価値
4. 購入可能性

追加資金と1回の注文額のplanning baselineは[`docs/portfolio-management.md`](./docs/portfolio-management.md)を正本とします。予算、cash、集中、保有・予約は人間へ見せるwarning/annotationであり、それだけで投資価値順位を変えません。候補がない、価格が最大許容価格を超える、一次情報が足りない場合は、買わずに終了することが正常な判断です。

## Human boundary

AIは提案までを担当し、人間だけが`approve / defer / reject`とbroker操作を行います。寄り前の価格提案にはJPX基盤の最新完全営業日のraw/unadjusted closeを使え、realtime quoteや板は必須ではありません。AIは人間から報告されていない`open / filled / cancelled`を推定せず、ledgerを更新しません。

## Two operating cycles

| cycle | 目的 | 主な成果物 |
| --- | --- | --- |
| continuous decision cycle | お買い得候補を見つけ、発注判断、結果反映、保有見直しまで進める | operation session、proposal、thesis/review、human-confirmed ledger、holding review、annual outcome |
| improvement loop | 見積り方法を計測し、再現可能な変更だけ採用する | preregistration、design/confirm評価、PR、operation test、dated report |

個別銘柄の判断と基盤方法の改善を同じ作業に混ぜません。日常運用で見つけた基盤不備はIssue化し、improvement loopへ渡します。

## Read-only 運用 UI

frontend を build して Baibai App を起動します。

```bash
cd ui
npm run build
cd ..
uv run baibai-app serve
```

ブラウザで `http://127.0.0.1:8712` を開きます。UI と API は application DB と各 store を read-only で参照し、task や portfolio を更新しません。

## Three layers

| layer | 内容 | 例 |
| --- | --- | --- |
| L1 observed data | 再取得可能な市場・開示データ | `data/screening/market.sqlite` |
| L2 derived / estimate | 決定論的screen、指標、E[r]、FV anchor | screening output、local opportunity workspace |
| L3 judgment / operation | 一次情報を確認した投資・保有判断 | macro context、thesis/review、proposal、ledger、operation session |

E[r]とFV anchorは決定論的でも事実ではなくestimateです。候補探索のlocal outputを判断の正本にせず、採用した入力と判断だけをapplication DBへpublishします。

## Repository map

| path | 役割 |
| --- | --- |
| `src/baibai_engine/` | domain、application service、application DB、read API |
| `src/baibai_app/` / `ui/` | read-only UI（Baibai App） |
| `method/` | Git 管理の screening rules / macro panel / research playbook |
| `data/` | application DBとrebuildable data/run store |
| `reports/` | dated 計測記録（改善ループの一次資料） |
| `docs/` | doctrine、governance、operations、workflow、reference |
| `.agents/skills/` | repository-local AI skillの正本 |
| `.claude/skills/` | canonical skillへのClaude互換symlink |
| `tools/drift/` | docs link・CLI・語彙・duplicate 定数・skill inventory の drift gate |
| `cloud/` | Cloudflare Worker と serving・deploy 設定 |
| `tests/` | domain、public CLI、DB/write-time contract test |

詳細は[`docs/architecture.md`](./docs/architecture.md)を参照してください。

## Public CLI

| command | 役割 |
| --- | --- |
| `baibai-engine screening` | run・select・shortlist・ticker-profile・cache 管理・calibration |
| `baibai-engine research` | opportunity workspace、thesis/review scaffold、promotion、前営業日指値 |
| `baibai-engine research evaluate` | thesisと既存execution policyの再計算 |
| `baibai-engine position` | ledger、typed draft/apply、holding review、portfolio outcome |
| `baibai-engine macro` | macro indicator seriesとcontext publication |
| `baibai-engine operation` | current operation workspaceとimmutable final result |
| `baibai-engine proposal` | trade proposalと人間のcurrent decision |
| `baibai-engine task` | task current state |
| `baibai-engine db` | application DB init/info/backup |
| `baibai-app` | 127.0.0.1固定のread-only UI（Baibai App） |

日常運用の完全なcommand順は[`docs/operations/decision-cycle.md`](./docs/operations/decision-cycle.md)、各optionはpublic `--help`を正本とします。

## Non-goals

- broker API、自動発注、未報告broker状態の推定
- realtime quoteや板を通常proposalの必須入力にすること
- ledgerをbroker会計の完全な複製へ発展させること
- 予算消化のために候補品質を下げること
- 短期screen成績の最適化、grid search、機械学習score
- ETF、投資信託、海外株、口座・税制の完全モデル化

## Development gates

Python 3.14と`uv`を使用します。model、DB、src、docsを変更したら次を実行します。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/uv-cache uv run mypy
UV_CACHE_DIR=/tmp/uv-cache uv run pytest
UV_CACHE_DIR=/tmp/uv-cache uv run lint-imports
```

詳細は[`docs/reference/python-foundation.md`](./docs/reference/python-foundation.md)と[`docs/reference/testing-and-validation.md`](./docs/reference/testing-and-validation.md)を参照してください。

## Issues

feature、bug、基盤改善、PR deliveryは[GitHub Issues](https://github.com/koumatsumoto/baibai-loop/issues)で管理します。運用taskの正本はapplication DBで、`baibai-engine task`から操作します。
