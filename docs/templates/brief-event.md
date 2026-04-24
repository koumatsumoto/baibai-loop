---
type: event
scope: world | japan | sector-xx
published_at: "YYYY-MM-DDTHH:MM:SS+09:00"
sources:
  - "URL1"
---

# Brief Event: YYYY-MM-DD {kind} ({slug})

**成分**: 4 成分アーキテクチャの **(a) マクロ事実ブリーフ**（不定期）([`/docs/components/brief.md`](/docs/components/brief.md))

**レイヤー**: 事実レイヤー（観測値・一次統計引用のみ。解釈・予測・相場観は書かない）

**Trigger**: 以下のいずれかに該当したときに作成:

- 主要統計が予想対比 ±10% 以上乖離
- 金融政策変更（利上げ・利下げ、YCC 調整等）
- 主要指数 ±3% 以上変動（Nikkei 225, S&P 500, 米 10Y ±15bp 等）
- 地政学 shock（戦争勃発、主要制裁、政権交代、中央銀行総裁交代等）

発生日: YYYY-MM-DD
観測日: YYYY-MM-DD

## 1. イベントの事実

### 1.1 何が発表/発生したか

- [事実記述のみ。「示唆」「受けて」「背景に」等の因果推論を避ける]
- 一次ソース: [機関名](URL) (YYYY-MM-DD取得)

### 1.2 主要数値（該当する場合）

| 項目 | 値 | 予想値 | 前回値 | ソース |
|---|---|---|---|---|
| [指標] | X.X | X.X | X.X | [機関名](URL) (YYYY-MM-DD取得) |

## 2. 市場反応の事実

イベント発表直後〜当営業日終了までの市場指標を記録。解釈は書かない。

| 指標 | 発表前 | 発表後 | 変化 | ソース |
|---|---|---|---|---|
| [主要指数] | X,XXX | X,XXX | +X.X% | [ソース](URL) (YYYY-MM-DD取得) |
| [関連金利] | X.XX% | X.XX% | +X bp | [ソース](URL) (YYYY-MM-DD取得) |
| [関連通貨] | XXX.XX | XXX.XX | +X.X% | [ソース](URL) (YYYY-MM-DD取得) |

## 3. 次回の関連イベント予定

- YYYY-MM-DD: [関連する次回の会合・発表] [ソース](URL) (YYYY-MM-DD取得)

---

記入ルールは [`/docs/workflow.md`](/docs/workflow.md) を、設計根拠は [`/docs/design-principles.md`](/docs/design-principles.md) を参照。
