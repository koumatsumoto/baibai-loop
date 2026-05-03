# Baibai-Loop

Baibai-Loop は、日本株トレードにおける戦略立案、スクリーニング、売買実行、事後検証を一貫して記録し、継続的に改善するためのリポジトリです。

単なる売買記録の保管場所ではなく、相場観や仮説、スクリーニング条件、売買理由、結果、振り返りを蓄積し、次の意思決定に活かすための運用基盤として使うことを目的としています。

## このリポジトリの目的

このリポジトリでは、以下のループを継続的に回すことを想定しています。

1. マクロ事実を蓄積する（brief）
2. マクロ見解を更新する（outlook）
3. スクリーニング基準でふるいにかける（screened）
4. 個別銘柄を深掘り調査する（research）
5. 条件を満たしたら取引する（trades）
6. 事後検証と retro で次回改善に活かす（reviews）

この流れを通じて、思いつきではなく、**検証可能な事実に基づいて売買判断を改善していく** ことを重視します。詳しくは [`docs/philosophy.md`](./docs/philosophy.md) と [`docs/architecture-v1.md`](./docs/architecture-v1.md) を参照してください。

## 対象とする売買スタイル

- 基本は **2 か月以内（5〜40 営業日）のスイングトレード**
- **マクロ 76% / ミクロ 24%** の比重で判断（大方針、運用途中で動かさない）
- **long-only**、裁量支援基盤
- 配当利回り / Rerating Book はスコープ外

## アーキテクチャ（4 成分 + 下流）

Baibai-Loop は 4 成分 + 下流（2 成分）で構成されます。

| 成分 | directory | 役割 |
| --- | --- | --- |
| a | [`records/01-brief/`](./records/01-brief/) | マクロ事実ブリーフ（定期+不定期） |
| b | `records/03-screened/` | スクリーニング通過銘柄 |
| c | `records/02-outlook/` | マクロ見解（brief を積み上げて作成） |
| d | `records/04-research/` | 個別銘柄リサーチ packet |
| ― | `records/05-trades/` | 執行記録 |
| ― | `records/06-reviews/` | 事後検証 |

**2 トラック構成**:

- **Macro track (独立)**: `records/01-brief/` → `records/02-outlook/`（売買イベントと独立に更新）
- **Micro track (売買ループ)**: `records/03-screened/` → `records/04-research/` → `records/05-trades/` → `records/06-reviews/` → retro feedback

全体像の詳細は [`docs/architecture-v1.md`](./docs/architecture-v1.md) を参照。

## リポジトリ名の背景

**Baibai-Loop** は「売買」+「Loop」の合成。単発の売買ではなく、以下の循環を回す場にしたいという意図があります。

`strategy -> screening -> execution -> review -> learning`

### 名前に込めたニュアンス

- 売買そのものよりも、売買を含む運用改善ループ
- 仮説を立て、条件を設計し、実行し、結果を評価し、次に活かす
- 「検証可能な事実をためる」「勝ち筋の型を育てる」ための基盤

## ディレクトリ構成

```
baibai-loop/
├── README.md                          # このファイル
├── docs/
│   ├── philosophy.md                  # 思想・ベースの考え方・進化の歴史
│   ├── architecture-v1.md             # 4 成分 + 下流アーキテクチャの正本
│   ├── design-principles.md           # 設計原則
│   ├── data-sources.md                # データソース（成分ごと）
│   ├── workflow.md                    # 日々の運用ワークフロー
│   ├── python-foundation.md           # Python 3.14 基盤と品質ゲート
│   ├── components/                    # 各成分の運用仕様
│   │   ├── brief.md
│   │   ├── screened.md
│   │   ├── outlook.md
│   │   ├── research.md
│   │   ├── trades.md
│   │   └── reviews.md
│   ├── screening/                     # スクリーニングサブシステム詳細
│   │   ├── principles.md
│   │   ├── failure-taxonomy.md
│   │   ├── universe-rules.md
│   │   ├── valuation-metrics.md
│   │   ├── mechanical-v1.md
│   │   └── macro-gate-procedure.md
│   └── templates/                     # 各成分の記入テンプレート
│       ├── brief-world-daily.yaml
│       ├── brief-world-weekly.yaml
│       ├── brief-japan-monthly.yaml
│       ├── brief-event.yaml
│       ├── outlook.yaml
│       ├── screened.yaml
│       ├── research.md
│       ├── trade.md
│       ├── review.md
│       ├── retro-monthly.md
│       └── playbook.md
├── records/
│   ├── 01-brief/                    # (a) マクロ事実ブリーフ
│   ├── 02-outlook/                  # (c) マクロ見解
│   ├── 03-screened/                 # (b) スクリーニング通過銘柄
│   ├── 04-research/                 # (d) 個別銘柄リサーチ packet
│   ├── 05-trades/                   # 執行記録
│   ├── 06-reviews/                  # 事後検証
│   ├── _data/                       # screening raw data / derived cache
│   ├── _ledger/                     # paper / skipped / updates ledger
│   ├── _playbooks/                  # 運用中の playbook
│   └── _schemas/                    # validation schema
├── src/
├── tests/
├── pyproject.toml
└── uv.lock
```

## 運用ルール

- 事実（`records/01-brief/`, `records/03-screened/`）と分析（`records/02-outlook/`, `records/04-research/`）を**物理的に分離**
- 分析階層は **世界情勢 → 日本経済 → 日本株**（`docs/design-principles.md`）
- 一次統計（中央銀行・政府・国際機関）中心で事実を記録、意見記事は取らない
- マクロゲートを通過した銘柄のみ research 対象（逆風銘柄は採用しない）
- Kill switch: 決算またぎ禁止 / 日銀会合前日禁止 / FOMC 前日禁止
- 詳細なルールは以下のドキュメントを参照:
  - 思想: [`docs/philosophy.md`](./docs/philosophy.md)
  - アーキテクチャ: [`docs/architecture-v1.md`](./docs/architecture-v1.md)
  - 設計原則: [`docs/design-principles.md`](./docs/design-principles.md)
  - データソース: [`docs/data-sources.md`](./docs/data-sources.md)
  - 運用手順: [`docs/workflow.md`](./docs/workflow.md)
  - Python 基盤: [`docs/python-foundation.md`](./docs/python-foundation.md)
  - 各成分: [`docs/components/`](./docs/components/)
  - スクリーニング: [`docs/screening/`](./docs/screening/)
  - テンプレート: [`docs/templates/`](./docs/templates/)

## 改善バックログ

改善点・未解決の設計課題・将来対応項目は GitHub Issues で管理する。`docs/` 配下に backlog.md などの追跡ファイルは置かない。作業引き継ぎ (handoff) も `docs/` には置かず、PR 本文か issue comment に記録する（`docs/` は normative spec 専用）。

- 現在の open issues: <https://github.com/koumatsumoto/baibai-loop/issues>
- 新規に課題を見つけたら issue を起票する。タイトルは「対象 + 問題 + 望ましい状態」の順で具体的に書く
- コード内で課題箇所に marker を残す場合は `FIXME(issue #N)` 形式で該当 issue 番号を入れる。全件は `git grep 'FIXME(issue'` で列挙できる
- 過去の移行記録: screening-automation-v1 の暫定 backlog (`docs/screening/backlog.md`、2026-04-24 削除) は issue #14-#17 に移行済み。by-design 項目と具体性不足項目 (data persistence 戦略・J-Quants methods 拡張) は移行せず見送り
