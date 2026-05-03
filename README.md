# Baibai-Loop

Baibai-Loop は、日本株トレードにおける戦略立案、スクリーニング、売買実行、事後検証を一貫して記録し、継続的に改善するためのリポジトリです。

単なる売買記録の保管場所ではなく、相場観や仮説、スクリーニング条件、売買理由、結果、振り返りを蓄積し、次の意思決定に活かすための運用基盤として使うことを目的としています。

## このリポジトリの目的

以下のループを継続的に回します。

1. マクロ事実を蓄積する（brief）
2. マクロ見解を更新する（outlook）
3. スクリーニング基準でふるいにかける（candidates）
4. 個別銘柄を深掘り調査する（research）
5. 条件を満たしたら取引する（trades）
6. 事後検証と retro で次回改善に活かす（reviews）

思いつきではなく、**検証可能な事実に基づいて売買判断を改善していく** ことを重視します。詳しくは [`docs/philosophy.md`](./docs/philosophy.md) と [`docs/architecture.md`](./docs/architecture.md) を参照してください。

## 対象とする売買スタイル

- 基本は **2 か月以内（5〜40 営業日）のスイングトレード**
- **マクロ 76% / ミクロ 24%** の比重で判断（運用途中で動かさない）
- **long-only**、裁量支援基盤
- 配当利回り / Rerating Book はスコープ外

## アーキテクチャ（4 成分 + 下流）

| 成分 | directory | 役割 |
| --- | --- | --- |
| a | [`records/01-brief/`](./records/01-brief/) | マクロ事実ブリーフ（定期+不定期） |
| b | `records/03-candidates/` | スクリーニング通過銘柄 |
| c | `records/02-outlook/` | マクロ見解（brief を積み上げて作成） |
| d | `records/04-research/` | 個別銘柄リサーチ packet |
| ― | `records/05-trades/` | 執行記録 |
| ― | `records/06-reviews/` | 事後検証 |

**2 トラック構成**:

- **Macro track (独立)**: `records/01-brief/` → `records/02-outlook/`（売買イベントと独立に更新）
- **Micro track (売買ループ)**: `records/03-candidates/` → `records/04-research/` → `records/05-trades/` → `records/06-reviews/` → retro feedback

詳細は [`docs/architecture.md`](./docs/architecture.md) を参照。

## ディレクトリ構成

```
baibai-loop/
├── README.md
├── AGENTS.md / CLAUDE.md              # AI agent 向け運用ルール
├── docs/
│   ├── philosophy.md                  # 思想・ベース概念
│   ├── architecture.md                # 4 成分 + 下流アーキテクチャの正本
│   ├── design-principles.md           # 設計原則
│   ├── data-sources.md                # データソース
│   ├── workflow.md                    # 日々の運用ワークフロー
│   ├── python-foundation.md           # Python 基盤と品質ゲート
│   ├── components/                    # 各成分の運用仕様
│   ├── screening/                     # スクリーニングサブシステム詳細
│   └── templates/                     # 記入テンプレート
├── records/
│   ├── 01-brief/                      # (a) マクロ事実ブリーフ
│   ├── 02-outlook/                    # (c) マクロ見解
│   ├── 03-candidates/                 # (b) スクリーニング通過銘柄
│   ├── 04-research/                   # (d) 個別銘柄リサーチ packet
│   ├── 05-trades/                     # 執行記録
│   ├── 06-reviews/                    # 事後検証
│   ├── _data/                         # screening raw data / derived cache
│   ├── _ledger/                       # paper / skipped / updates ledger
│   ├── _playbooks/                    # 運用中の playbook
│   └── _schemas/                      # validation schema
├── src/                               # baibai-loop CLI 実装
├── tests/
├── pyproject.toml
└── uv.lock
```

## 運用ルール（要点）

- 事実（`records/01-brief/`, `records/03-candidates/`）と分析（`records/02-outlook/`, `records/04-research/`）を**物理的に分離**
- 分析階層は **世界情勢 → 日本経済 → 日本株**
- 一次統計（中央銀行・政府・国際機関）中心で事実を記録、意見記事は取らない
- マクロゲートを通過した銘柄のみ research 対象（逆風銘柄は採用しない）
- Kill switch: 決算またぎ禁止 / 日銀会合前日禁止 / FOMC 前日禁止

詳細は以下を参照:

- 思想: [`docs/philosophy.md`](./docs/philosophy.md)
- アーキテクチャ: [`docs/architecture.md`](./docs/architecture.md)
- 設計原則: [`docs/design-principles.md`](./docs/design-principles.md)
- データソース: [`docs/data-sources.md`](./docs/data-sources.md)
- 運用手順: [`docs/workflow.md`](./docs/workflow.md)
- Python 基盤: [`docs/python-foundation.md`](./docs/python-foundation.md)
- 各成分: [`docs/components/`](./docs/components/)
- スクリーニング: [`docs/screening/`](./docs/screening/)
- テンプレート: [`docs/templates/`](./docs/templates/)

## CLI

```bash
uv run baibai-loop-screening run --asof YYYY-MM-DD     # candidates 生成
uv run baibai-loop-screening select --asof YYYY-MM-DD  # outlook と突合した候補ランキング
uv run baibai-loop-validate                            # records と schema の整合検査
uv run baibai-loop-ledger sync --root .                # research decision を ledger に正規化
```

実装詳細は [`docs/screening/automation.md`](./docs/screening/automation.md) と [`docs/components/ledger.md`](./docs/components/ledger.md)。

## 改善バックログ

- 改善点・未解決の設計課題は GitHub Issues で管理する: <https://github.com/koumatsumoto/baibai-loop/issues>
- 新規に課題を見つけたら issue を起票する。タイトルは「対象 + 問題 + 望ましい状態」の順で具体的に書く
- コード内で課題箇所に marker を残す場合は `FIXME(issue #N)` 形式で該当 issue 番号を入れる
- `docs/` は normative spec 専用。backlog や handoff は置かず、PR 本文か issue comment に記録する
