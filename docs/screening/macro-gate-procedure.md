# screening/macro-gate-procedure.md

Baibai-Loop の **Macro regime gate 判定手順**。research の front matter `macro_regime_gate` を決める手順で、`records/03-outlook/` を唯一の source として運用する。

## 1. 位置付け

- `records/03-outlook/` と `records/05-research/` の接続点
- philosophy 柱 2（macro regime discipline）を具体運用に落とす rule
- research 採用判定の**必須通過ゲート**

## 2. 判定の 2 階層

Macro regime gate は、outlook 由来の **regime status** と、候補に対する **decision effect** を分けて記録する。

| 階層 | 値 | 意味 |
| --- | --- | --- |
| Regime status | `supportive` / `neutral` / `adverse` / `unknown` | outlook input の市場環境 |
| Decision effect | `pass` / `conditional` / `block` | 当該候補の採用・sizing に与える効果 |

`supportive` / `neutral` / `adverse` は outlook の `sectors` / `exposure_buckets` / `security_exposures` から reducer で合成する。`unknown` や expired input は、情報不足として `conditional` または `block` に倒す。

## 3. 判定手順

### 3.1 通常運用（outlook が最新）

1. 対象銘柄の **業種（東証 33 業種）** と **security_exposures** を確認する
2. 最新 `records/03-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml` の `sectors` / `exposure_buckets` を参照し、対象 scope の input を集める
3. input ごとに `scope`, `key`, `status`, `source_ref`, `horizon`, `valid_until` を `macro_regime_gate.inputs[]` に残す
4. reducer で `aggregate_status` と `decision_effect` を算出する
5. research front matter の `macro_regime_gate`, `outlook_ref`, `brief_refs` に記録する

**null / unknown の扱い**: outlook の `sectors` / `exposure_buckets` で対象 scope が `null` または欠損の場合、該当 input は `unknown` として扱う。`unknown` が含まれる場合、approved には exposure verification または conditional cap を要求する。

### 3.2 食い違い時の保守側優先ルール

複数 input が食い違う場合、以下の優先順位で **保守的な方**を採用:

```
adverse > unknown > neutral > supportive
```

例:
- 業種 `supportive` × exposure bucket `neutral` → `aggregate_status: neutral`
- 業種 `neutral` × exposure bucket `adverse` → `aggregate_status: adverse`
- 業種 `supportive` × security exposure `unknown` → `aggregate_status: unknown`

保守側優先の理由:
- 参照 input のうち片方が逆風なら、全体のリスクは少なくとも neutral 以下とみなす
- supportive 採用の誤判定が採用不可になるのは想定内、逆は structural trap に繋がる

### 3.3 Decision effect reducer

| aggregate_status | input freshness | policy | decision_effect |
| --- | --- | --- | --- |
| `supportive` | valid | 通常 | `pass` |
| `neutral` | valid | 通常 | `conditional` |
| `unknown` | valid / incomplete | 通常 | `conditional` |
| `adverse` | valid | `adverse_treatment: conditional` | `conditional` |
| `adverse` | valid | `adverse_treatment: block` | `block` |
| 任意 | expired / missing outlook | 任意 | `block` |

`decision_effect: conditional` で approved にする場合は、position cap、blocking condition、または `policy_overrides[]` に例外理由を構造化する。

### 3.4 outlook 未更新時の対応

- **最新 outlook が古く**、その後に重大 brief（BOJ/FOMC/CPI 大振れ等）が出た場合:
  - 該当 brief を research の `brief_refs` に追加
  - `macro_regime_gate.inputs[]` に該当 brief を追加し、reducer を再実行する
  - `decision_effect` は保守側にのみ変更可（`pass` → `conditional` / `block`, `conditional` → `block`）
  - 逆方向の上書きは不可
- 常態的に outlook が遅れるなら、outlook の更新 trigger を見直す（[`../components/outlook.md`](../components/outlook.md)）

### 3.5 outlook が存在しない場合

- `records/03-outlook/` に最新の outlook がない場合は、research 作成前に outlook を作成する（[`../components/outlook.md`](../components/outlook.md) §3 初回作成手順）
- outlook 作成完了前に research を作成してはならない（gate 判定不能のため）

## 4. Front matter 記録

### 4.1 research 側（必須）

```yaml
macro_regime_gate:
  aggregate_status: supportive | neutral | adverse | unknown
  decision_effect: pass | conditional | block
  source_scope: sector | exposure_bucket | security_exposure | mixed
  reducer_id: macro-regime-reducer-v1
  inputs:
    - scope: sector
      key: 情報・通信業
      status: supportive
      source_ref: records/03-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml
      horizon: 30bd
      valid_until: YYYY-MM-DD
outlook_ref: records/03-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml       # 必須
brief_refs:                                         # 任意（outlook 後の緊急 brief があった場合のみ）
  - records/02-brief/YYYY/MM/YYYY-MM-DD-*.yaml
```

### 4.2 記録例

**パターン 1: 通常運用（outlook のみ参照）**:

```yaml
macro_regime_gate:
  aggregate_status: supportive
  decision_effect: pass
  source_scope: sector
  reducer_id: macro-regime-reducer-v1
  inputs:
    - scope: sector
      key: 情報・通信業
      status: supportive
      source_ref: records/03-outlook/2026/04/outlook-2026-04-25-q2-outlook.yaml
      horizon: 30bd
      valid_until: '2026-05-25'
outlook_ref: records/03-outlook/2026/04/outlook-2026-04-25-q2-outlook.yaml
# brief_refs は省略可
```

**パターン 2: outlook 後に緊急 brief で gate 下方修正**:

```yaml
macro_regime_gate:
  aggregate_status: neutral
  decision_effect: conditional
  source_scope: mixed
  reducer_id: macro-regime-reducer-v1
  inputs:
    - scope: sector
      key: 情報・通信業
      status: supportive
      source_ref: records/03-outlook/2026/04/outlook-2026-04-25-q2-outlook.yaml
      horizon: 30bd
      valid_until: '2026-05-25'
    - scope: event
      key: boj-tightening
      status: neutral
      source_ref: records/02-brief/2026/04/2026-04-28-boj-tightening.yaml
      horizon: 10bd
      valid_until: '2026-05-12'
outlook_ref: records/03-outlook/2026/04/outlook-2026-04-25-q2-outlook.yaml
brief_refs:
  - records/02-brief/2026/04/2026-04-28-boj-tightening.yaml  # outlook 後の緊急 brief
```

## 5. 採用判定への影響

| `macro_regime_gate.decision_effect` | 採用可否 | 条件 |
| --- | --- | --- |
| `pass` | 採用可 | evidence、payoff、反対仮説、policy cap で最終判定 |
| `conditional` | 条件付き採用可 | sizing cap、blocking condition、例外理由を構造化する |
| `block` | 採用不可 | research decision は rejected / deferred に倒す |

## 6. Retro での評価

月次 retro で以下を集計（[`../components/reviews.md`](../components/reviews.md)）:

- **追い風判定銘柄**の +15/+30 営業日パフォーマンス（想定通り上昇したか）
- **逆風判定で見送った銘柄**のパフォーマンス（見送り判断の妥当性、過度に保守的でないか）
- **gate 判定誤り**（採用時 supportive → 保有中 adverse に反転）の頻度

これらから outlook の更新 trigger や gate 判定の精度を評価し、次周回の改善項目にする。

## 7. 参考

- [`principles.md`](./principles.md): スクリーニング原則
- [`../components/outlook.md`](../components/outlook.md): outlook 運用仕様
- [`../components/research.md`](../components/research.md): research 選定プロセス
- [`../philosophy.md`](../philosophy.md): 思想（macro regime discipline）
