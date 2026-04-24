# Baibai-Loop

Baibai-Loop は、日本株トレードにおける戦略立案、スクリーニング、売買実行、事後検証を一貫して記録し、継続的に改善するためのリポジトリです。

単なる売買記録の保管場所ではなく、相場観や仮説、スクリーニング条件、売買理由、結果、振り返りを蓄積し、次の意思決定に活かすための運用基盤として使うことを目的としています。

## このリポジトリの目的

このリポジトリでは、以下のループを継続的に回すことを想定しています。

1. マクロ事実を蓄積する（brief）
2. マクロ見解を更新する（view）
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
| a | [`brief/`](./brief/) | マクロ事実ブリーフ（定期+不定期） |
| b | `screened/` | スクリーニング通過銘柄（初回ファイル生成で作成） |
| c | `view/` | マクロ見解（brief を積み上げて作成、初回ファイル生成で作成） |
| d | `research/` | 個別銘柄リサーチ packet（初回ファイル生成で作成） |
| ― | `trades/` | 執行記録（初回ファイル生成で作成） |
| ― | `reviews/` | 事後検証（初回ファイル生成で作成） |

**2 トラック構成**:

- **Macro track (独立)**: `brief/` → `view/`（売買イベントと独立に更新）
- **Micro track (売買ループ)**: `screened/` → `research/` → `trades/` → `reviews/` → retro feedback

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
│   ├── components/                    # 各成分の運用仕様
│   │   ├── brief.md
│   │   ├── screened.md
│   │   ├── view.md
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
│       ├── brief-world-weekly.md
│       ├── brief-japan-monthly.md
│       ├── brief-event.md
│       ├── view.md
│       ├── screened.md
│       ├── research.md
│       ├── trade.md
│       ├── review.md
│       ├── retro-monthly.md
│       └── playbook.md
├── brief/                             # (a) マクロ事実ブリーフ
│   ├── README.md
│   └── 2026/{01..04}/...
├── playbooks/                         # 運用中の playbook
│   ├── README.md
│   ├── valuation-mean-reversion-v1.md
│   └── valuation-catalyst-confirmation-v1.md
# 以下は初回ファイル生成で自然発生する（本リポジトリではまだ作成しない）:
# ├── screened/
# ├── view/
# ├── research/
# ├── trades/
# └── reviews/
```

## 運用ルール

- 事実（`brief/`, `screened/`）と分析（`view/`, `research/`）を**物理的に分離**
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
  - 各成分: [`docs/components/`](./docs/components/)
  - スクリーニング: [`docs/screening/`](./docs/screening/)
  - テンプレート: [`docs/templates/`](./docs/templates/)
