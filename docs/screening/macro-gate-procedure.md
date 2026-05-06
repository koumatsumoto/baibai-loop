# screening/macro-gate-procedure.md

Baibai-Loop の **Macro gate 判定手順**。research の front matter `macro_gate` を決める手順で、`records/02-outlook/` を唯一の source として運用する。

## 1. 位置付け

- `records/02-outlook/` と `records/04-research/` の接続点
- philosophy 柱 2（macro regime discipline）を具体運用に落とす rule
- research 採用判定の**必須通過ゲート**

## 2. 判定の 3 段階

| 判定 | 意味 | research 採用可否 |
| --- | --- | --- |
| `tailwind` | 追い風 | 優先採用 |
| `neutral` | 中立 | 条件付き採用（security-level confidence が高い場合） |
| `headwind` | 逆風 | **原則採用不可**（valuation trap リスク） |

## 3. 判定手順

### 3.1 通常運用（outlook が最新）

1. 対象銘柄の **業種（東証 33 業種）** と **地域（domestic / external-demand / us / emerging 等）** を確認
2. 最新 `records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml` の `sectors` / `regions` を参照し、業種と地域の判定を取得
3. 食い違いがある場合は **保守的な方を採用**（下記 3.2）
4. research front matter の `macro_gate` と `outlook_ref` に記録

**null フィールドの扱い**: outlook の `sectors` / `regions` で対象業種/地域が `null`（判定未記入）の場合、Macro gate は **`neutral` 扱い** とする。情報不足で `headwind` 側に倒さない（採用率の過度な低下を避けるため）。outlook が充実してきたら `null` を削り、明示的な判定に更新する。この運用は [`../components/outlook.md`](../components/outlook.md) §2.4 と整合。

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

### 3.3 outlook 未更新時の対応

- **最新 outlook が古く**、その後に重大 brief（BOJ/FOMC/CPI 大振れ等）が出た場合:
  - 該当 brief を research の `brief_refs` に追加
  - gate 判定を **保守側にのみ** 手動上書き可
    - tailwind → neutral ✓
    - tailwind → headwind ✓
    - neutral → headwind ✓
    - **逆方向の上書き不可**（neutral → tailwind、headwind → neutral は禁止）
- 常態的に outlook が遅れるなら、outlook の更新 trigger を見直す（[`../components/outlook.md`](../components/outlook.md)）

### 3.4 outlook が存在しない場合

- `records/02-outlook/` に最新の outlook がない場合は、research 作成前に outlook を作成する（[`../components/outlook.md`](../components/outlook.md) §3 初回作成手順）
- outlook 作成完了前に research を作成してはならない（gate 判定不能のため）

## 4. Front matter 記録

### 4.1 research 側（必須）

```yaml
macro_gate: tailwind | neutral | headwind
outlook_ref: records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml       # 必須
brief_refs:                                         # 任意（outlook 後の緊急 brief があった場合のみ）
  - records/01-brief/YYYY/MM/YYYY-MM-DD-*.yaml
```

### 4.2 記録例

**パターン 1: 通常運用（outlook のみ参照）**:

```yaml
macro_gate: tailwind
outlook_ref: records/02-outlook/2026/04/outlook-2026-04-25-q2-outlook.yaml
# brief_refs は省略可
```

**パターン 2: outlook 後に緊急 brief で gate 下方修正**:

```yaml
macro_gate: neutral           # outlook は tailwind だったが brief で neutral に下方
outlook_ref: records/02-outlook/2026/04/outlook-2026-04-25-q2-outlook.yaml
brief_refs:
  - records/01-brief/2026/04/2026-04-28-boj-tightening.yaml  # outlook 後の緊急 brief
```

## 5. 採用判定への影響

| `macro_gate` | 採用可否 | 条件 |
| --- | --- | --- |
| `tailwind` | 採用可 | 4 軸評価 + 反対仮説 + kill switch で最終判定 |
| `neutral` | 条件付き採用可 | evidence の重なり、割安度、反対仮説、catalyst の有無で confidence 高いもののみ |
| `headwind` | **原則採用不可** | 例外運用は playbook 改訂議論の input にする |

## 6. Retro での評価

月次 retro で以下を集計（[`../components/reviews.md`](../components/reviews.md)）:

- **追い風判定銘柄**の +15/+30 営業日パフォーマンス（想定通り上昇したか）
- **逆風判定で見送った銘柄**のパフォーマンス（見送り判断の妥当性、偽陰性率）
- **gate 判定誤り**（採用時 tailwind → 保有中 headwind に反転）の頻度

これらから outlook の更新 trigger や gate 判定の精度を評価し、次周回の改善項目にする。

## 7. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`../components/outlook.md`](../components/outlook.md): outlook 運用仕様
- [`../components/research.md`](../components/research.md): research 選定プロセス
- [`../philosophy.md`](../philosophy.md): 思想（macro regime discipline）
