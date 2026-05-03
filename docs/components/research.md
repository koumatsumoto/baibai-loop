# components/research.md

Baibai-Loop 4 成分アーキテクチャの **(d) 個別銘柄リサーチ** の運用仕様。candidates × outlook から選定した個別銘柄の深掘り packet 本体で、本計画 v1 の主戦場。全体構造は [`../architecture-v1.md`](../architecture-v1.md)、スクリーニング詳細は [`../screening/`](../screening/) を参照。

## 1. 役割

- `records/03-candidates/` × `records/02-outlook/` から選定した個別銘柄について、**一時的割安の原因仮説・反対仮説・catalyst・crowding を深く分析**
- 採用判定 / 見送り / 保留を記録（packet）
- Micro track の分析層、4 成分統合の出力点

## 2. 選定プロセス（candidates × outlook → 候補絞り込み）

本節は v1 アーキテクチャの中核。`candidates` (ミクロ事実) × `outlook` (マクロ見解) の 2 軸統合を具体化する。

### 2.1 4 ステップ

1. **最新 candidates を取得**: 直近の `records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml` を選び、`tickers` 配列を取得
2. **最新 outlook を参照**: 直近の `records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml` を選び、`sectors` / `regions` を取得
3. **gate 通過銘柄に絞り込み**: candidates ticker のうち、所属業種/地域が outlook で **tailwind または neutral** のものを候補に残す（**headwind は除外**）。candidates は `sector_33` のみ持つので、各業種を outlook の region (`japan-external-demand` 等) に対応させるには [`../screening/sector-region-map.md`](../screening/sector-region-map.md) の default mapping を出発点にする (mixed 業種は研究で個別判断)
4. **候補から人間 + AI が個別 ticker を選定**: 以下の基準で優先度判定
   - `threshold_hit` の重なり（複数閾値で hit した方が confidence 高）
   - valuation 指標の乖離幅（業種中央値比・過去自己比較）
   - 同業種の中で相対的に過剰売られ
   - P-A / P-B どちらの playbook に該当しそうか
   - 最大で 1 回の selection あたり **3-5 銘柄** に絞る（1 人運用で深掘りできる現実的な上限）

### 2.2 outlook 未更新時の対応

- 最新 outlook が古く、その後に重大 brief が出て gate 判定に影響する場合:
  - 該当 brief を research の `brief_refs` に追加
  - gate を **保守側にのみ** 手動上書き可（tailwind → neutral、neutral → headwind。逆方向の上書き不可）
  - 詳細: [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md)

### 2.3 outlook が存在しない期間（Bootstrap 前）

- `records/02-outlook/` に最新の outlook がない場合は、research 作成前に outlook を更新する
- bootstrap 手順: [`outlook.md`](./outlook.md) の Bootstrap 規則を参照

## 3. Path と命名

```
records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
```

- `<ticker>`: 4 文字の英数字文字列
- `<playbook>`: `valuation-mean-reversion-v1` (P-A) or `valuation-catalyst-confirmation-v1` (P-B)

## 4. Front matter 必須項目

```yaml
---
ticker: "7203"
name: "トヨタ自動車"
playbook: valuation-mean-reversion-v1 | valuation-catalyst-confirmation-v1
decision: accepted | skipped | pending
candidates_ref: records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml      # 必須
outlook_ref: records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml       # 必須（Bootstrap 後は例外なし）
brief_refs:                                        # 任意、outlook 後に出た緊急 brief 時のみ
  - records/01-brief/YYYY/MM/YYYY-MM-DD-*.yaml
ai-draft: true | false                             # AI 下書きフラグ
published_at: "ISO 8601"
tradable_at: "ISO 8601"
macro_gate: tailwind | neutral | headwind          # outlook 判定結果
macro_gate_override: "..."                         # headwind 採用時のみ必須
position_size_oku: 0.01                            # 建玉 proxy (億円)
adv_participation_pct: 0.2                         # position_size_oku / avg_turnover_oku * 100
market_cap_oku: 936                                # candidates 由来の時価総額 (億円)
sector_33: "情報・通信業"                         # candidates 由来の東証 33 業種
valuation:
  per_forward: 数値 | null                         # 会社予想ベース、未公表は null
  per_trailing: 数値
  pbr: 数値
  ev_ebitda: 数値
  p_s: 数値
  pcfr: 数値
  primary_metric: ["per_forward", "pbr"]          # この軸で判断に最も効いた 1-2 指標
---
```

- `outlook_ref` は **必須**（Bootstrap 後は例外なし）
- `brief_refs` は任意。outlook 後に gate 判定に影響する緊急 brief を参照した場合のみ追加
- `macro_gate` が `headwind` の場合は採用不可（原則）
- `decision: accepted` かつ `macro_gate: headwind` の場合は `macro_gate_override` が必須
- `position_size_oku` は仮定資本 1 億円ベース。採用 position 1.0% は `0.01` 億円として記録する
- `adv_participation_pct` は `5.0` 以上で hard reject
- `market_cap_oku` / `sector_33` は candidates から転記し、tier rule と sector 集中 warning の検証に使う
- 配当利回りは v1 スコープ外のため front matter に含めない

## 5. Packet 必須項目（本文、13 項目）

本文は [`../templates/research.md`](../templates/research.md) の 13 項目を使う:

```
1. Thesis（一文、why now / why this stock、マクロゲート × valuation 軸を明示）
2. Macro gate（tailwind / neutral / headwind）+ 判定根拠（outlook_ref 引用、必要なら brief_refs も + 1-2 行要約）【最上位ゲート】
3. Valuation snapshot（PER/PBR/EV-EBITDA/P-S/PCFR の 4 軸評価表、業種中央値・過去3年パーセンタイル・primary metric 付き）
4. 一時的割安の原因仮説（P-A 必須、P-B もできれば記入）
5. 反対仮説 - 構造的理由（8 例示 + 自由記述必須）
6. Catalyst（P-B 必須、種別 + 経過営業日 + 一次ソース URL）
7. Price reaction（前日比・週比・60 営業日比・出来高比）
8. Crowding（空売り残高 / 日々公表信用 / 特別注意 / 貸借状態、絶対値 + 60 日推移）
9. ミクロ 4 軸寄与度表（各軸: strong / weak / neutral）
10. Entry 条件（価格レンジ・日付・トリガー）
11. Exit 条件（利確目標・損切り・時間切れ = 最長 40 営業日）
12. Invalidation + Pre-mortem（3 営業日以内の無効化シナリオ、マクロゲート reversal 含む）
13. Position size（0.5% / 1% / 2% から選択、時価総額別上限）+ 採用判定（採用 / 見送り / 保留）+ 判定理由
```

詳細は [`../screening/principles.md`](../screening/principles.md) を参照。

### 5.1 schema 検証

front matter の必須 field と `playbook` ごとの本文 section 構造は `baibai-loop-validate` で検査される。playbook 別の本文 section schema は [`/records/_playbooks/`](/records/_playbooks/) 配下に `<name>.schema.yaml` として分離してあり、新 playbook を追加した時は同名 schema YAML を置くだけで validate に反映される (validate 本体改修不要)。CI の `Validate artefacts` step で merge gate になる。手元では `uv run baibai-loop-validate --target research` で個別に走らせられる。

research decision は `baibai-loop-ledger sync` で [`records/_ledger/`](./ledger.md) の JSONL に正規化される。`accepted` と `pending` は paper ledger、`skipped` は skipped ledger に残す。

## 6. 採用判定

### 6.1 判定カテゴリ

- **採用**: entry 準備に進む（次は `records/05-trades/` を作成）
- **見送り**: 採用しないが、skipped trade log として追跡（月次 retro で adverse selection 測定）
- **保留**: 一時的な情報不足。次回 research cycle で再評価

### 6.2 判定基準

- `macro_gate = headwind` は原則採用不可（bootstrap outlook 内で対象業種/地域が `null` の場合は neutral 扱いで判定可）
- valuation 軸で割安判定（業種中央値・過去自己比較）が成立
- 反対仮説を考えて「構造的 trap ではない」と確信できる
- catalyst（P-B のみ必須）が freshness ≦ 60 営業日
- crowding が踏み上げリスクと逆回転リスクの両方で許容範囲
- kill switch に抵触しない（決算またぎ / 日銀会合前日 / FOMC 前日）
- position size が時価総額別上限を満たす
- 市場規模 200-500 億帯で P-A (`valuation-mean-reversion-v1`) を採用する場合は `macro_gate_override` で明示理由を残す

## 7. trades への接続

採用した research の front matter path は、`records/05-trades/` の `research_ref` で参照される:

```yaml
# records/05-trades/2026/04/2026-04-26-7203.md
---
research_ref: records/04-research/2026/04/2026-04-25-7203-valuation-mean-reversion-v1.md
...
---
```

## 8. AI の役割境界（packet 項目単位）

| 項目 | 内容 | AI 可 | 人間のみ |
| --- | --- | --- | --- |
| 1 | Thesis ドラフト | ○ | 最終確認 |
| 2 | Macro gate 判定ドラフト | ○ | **確定は人間** |
| 3 | Valuation snapshot 数値取得・中央値比較 | ○ | 数値正誤確認 |
| 4 | 一時的割安の原因仮説ドラフト | ○ | 確定 |
| 5 | 反対仮説ドラフト | ○ | 確定 |
| 6 | Catalyst 種別・経過営業日ドラフト | ○ | **一次ソース URL 確認は人間** |
| 7 | Price reaction 機械集計 | ○ | |
| 8 | Crowding 指標取得 | ○ | |
| 9 | 4 軸寄与度初期評価 | ○ | 確定 |
| 10 | Entry 条件ドラフト | ○ | 確定 |
| 11 | Exit 条件ドラフト | ○ | 確定 |
| 12 | Invalidation / Pre-mortem ドラフト | ○ | 採用条件確定 |
| 13 | Position size 判定ドラフト | ○ | **最終採用判定は人間** |

AI 下書きは front matter `ai-draft: true` で識別、人間確認後 `false` に更新。

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位 76/24、事実と分析の分離）
- [`../architecture-v1.md`](../architecture-v1.md): 全体構造
- [`candidates.md`](./candidates.md): source となる candidates の仕様
- [`outlook.md`](./outlook.md): Macro gate source の仕様
- [`../screening/principles.md`](../screening/principles.md): Playbook P-A / P-B 定義
- [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md): Macro gate 判定手順
- [`../templates/research.md`](../templates/research.md): template（packet 13 項目）
- [`/records/_playbooks/valuation-mean-reversion-v1.md`](/records/_playbooks/valuation-mean-reversion-v1.md): P-A 本体
- [`/records/_playbooks/valuation-catalyst-confirmation-v1.md`](/records/_playbooks/valuation-catalyst-confirmation-v1.md): P-B 本体
