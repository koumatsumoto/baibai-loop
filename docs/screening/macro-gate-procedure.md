# screening/macro-gate-procedure.md

Baibai-Loop の **Macro gate 判定手順**。research の front matter `macro_gate` を決める手順で、`(c) view/` を唯一の source として運用する（v1 簡易版）。将来 `analysis/` 集約層に差し替え可能な設計にしておく。

## 1. 位置付け

- 4 成分アーキテクチャの (c) `view/` と (d) `research/` の接続点
- philosophy 柱 2（マクロ優位 76/24）を具体運用に落とす rule
- research 採用判定の**必須通過ゲート**

## 2. 判定の 3 段階

| 判定 | 意味 | research 採用可否 |
| --- | --- | --- |
| `tailwind` | 追い風 | 優先採用 |
| `neutral` | 中立 | 条件付き採用（ミクロの confidence が高い場合） |
| `headwind` | 逆風 | **原則採用不可**（valuation trap リスク） |

## 3. 判定手順

### 3.1 通常運用（view が最新）

1. 対象銘柄の **業種（東証 33 業種）** と **地域（domestic / external-demand / us / emerging 等）** を確認
2. 最新 `view/YYYY/MM/view-YYYY-MM-DD-*.md` の `sectors` / `regions` を参照し、業種と地域の判定を取得
3. 食い違いがある場合は **保守的な方を採用**（下記 3.2）
4. research front matter の `macro_gate` と `view_ref` に記録

### 3.2 食い違い時の保守側優先ルール

業種判定と地域判定が食い違う場合、以下の優先順位で **保守的な方**を採用:

```
headwind > neutral > tailwind
```

例:
- 業種 `tailwind` × 地域 `neutral` → 採用: `neutral`
- 業種 `neutral` × 地域 `headwind` → 採用: `headwind`
- 業種 `tailwind` × 地域 `headwind` → 採用: `headwind`

保守側優先の理由:
- 2 軸のうち片方が逆風なら、全体のリスクは少なくとも neutral 以下とみなす
- tailwind 採用の誤判定が採用不可になるのは想定内、逆は structural trap に繋がる

### 3.3 view 未更新時の対応

- **最新 view が古く**、その後に重大 brief（BOJ/FOMC/CPI 大振れ等）が出た場合:
  - 該当 brief を research の `brief_refs` に追加
  - gate 判定を **保守側にのみ** 手動上書き可
    - tailwind → neutral ✓
    - tailwind → headwind ✓
    - neutral → headwind ✓
    - **逆方向の上書き不可**（neutral → tailwind、headwind → neutral は禁止）
- 常態的に view が遅れるなら、view の更新 trigger を見直す（[`../components/view.md`](../components/view.md)）

### 3.4 view が存在しない期間（Bootstrap 前）

- v1 運用 Day 1 時点では `view/` が存在しない
- この場合、**先に view の bootstrap を実施** する（[`../components/view.md`](../components/view.md) の Bootstrap 規則）
- bootstrap 完了前に research を作成してはならない（gate 判定不能のため）

## 4. Front matter 記録

### 4.1 research 側（必須）

```yaml
macro_gate: tailwind | neutral | headwind
view_ref: view/YYYY/MM/view-YYYY-MM-DD-*.md       # 必須
brief_refs:                                         # 任意（view 後の緊急 brief があった場合のみ）
  - brief/YYYY/MM/event-YYYY-MM-DD-*.md
```

### 4.2 記録例

**パターン 1: 通常運用（view のみ参照）**:

```yaml
macro_gate: tailwind
view_ref: view/2026/04/view-2026-04-25-q2-outlook.md
# brief_refs は省略可
```

**パターン 2: view 後に緊急 brief で gate 下方修正**:

```yaml
macro_gate: neutral           # view は tailwind だったが brief で neutral に下方
view_ref: view/2026/04/view-2026-04-25-q2-outlook.md
brief_refs:
  - brief/2026/04/2026-04-28-boj-tightening.md  # view 後の緊急 brief
```

## 5. 採用判定への影響

| `macro_gate` | 採用可否 | 条件 |
| --- | --- | --- |
| `tailwind` | 採用可 | 4 軸評価 + 反対仮説 + kill switch で最終判定 |
| `neutral` | 条件付き採用可 | valuation の割安度 + catalyst freshness（P-B）で confidence 高いもののみ |
| `headwind` | **原則採用不可** | 例外運用は playbook v2 改訂議論の input にする（v1 は例外なし） |

## 6. 将来の置換想定（`analysis/` 集約層）

### 6.1 置換の動機

- 現状の `view/` は brief からの手動集約であり、運用負荷が高い
- 将来 `analysis/` 集約層を導入し、産業別・地域別の長期トレンドを AI 分析で集約する（philosophy §6 未熟さ 1）

### 6.2 置換の方法（将来）

- `view_ref` を `analysis_ref` に置き換える
- `analysis/YYYY/MM/analysis-YYYY-MM-DD-*.md` の schema は `view/` と互換性を持たせる想定
- v1 運用中に `view/` schema を stable に保つことで、将来の置換コストを下げる

### 6.3 v1 では置換しない

- 実装は future work、v1 運用 1 サイクル完走後に検討
- 本計画 v1 のスコープ外

## 7. Retro での評価

月次 retro で以下を集計（[`../components/reviews.md`](../components/reviews.md)）:

- **追い風判定銘柄**の +15/+30 営業日パフォーマンス（想定通り上昇したか）
- **逆風判定で見送った銘柄**のパフォーマンス（見送り判断の妥当性、偽陰性率）
- **gate 判定誤り**（採用時 tailwind → 保有中 headwind に反転）の頻度

これらから view の更新 trigger や gate 判定の精度を評価し、次周回の改善項目にする。

## 8. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`../components/view.md`](../components/view.md): view 運用仕様（Bootstrap 規則含む）
- [`../components/research.md`](../components/research.md): research 選定プロセス
- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位 76/24）
