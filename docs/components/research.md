# components/research.md

Baibai-Loop 4 成分アーキテクチャの **(d) 個別銘柄リサーチ** の運用仕様。candidates × outlook から選定した個別銘柄の深掘り packet 本体。全体構造は [`../architecture.md`](../architecture.md)、スクリーニング詳細は [`../screening/`](../screening/) を参照。

## 1. 役割

- `records/03-candidates/` × `records/02-outlook/` から選定した個別銘柄について、**割安の原因仮説・反対仮説・catalyst・crowding・株主還元を深く分析**
- 採用判定 / 見送り / 保留を記録（packet）
- Micro track の分析層、4 成分統合の出力点

## 2. 選定プロセス（candidates × outlook → 候補絞り込み）

1. **最新 candidates を取得**: 直近の `records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml` を選び、`candidates` 配列を取得
2. **最新 outlook を参照**: 直近の `records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml` を選び、`sectors` / `regions` を取得
3. **gate 通過銘柄に絞り込み**: candidates ticker のうち、所属業種/地域が outlook で **tailwind または neutral** のものを候補に残す（**headwind は除外**）。candidates は `sector_33` のみ持つので、各業種を outlook の region に対応させるには [`../screening/sector-region-map.md`](../screening/sector-region-map.md) の default mapping を出発点にする
4. **候補から人間 + AI が個別 ticker を選定**: 以下の基準で優先度判定
   - `select` の `candidates`（lane 分散済みの research 着手候補）を先に見る。単純な global rank を確認したい場合は `ranked_candidates` を見る
   - `select` の `selection_lane` と `selection_metrics`（primary thesis としてどの割安仮説を深掘りするか）
   - `select` の `recommendation_lane`（lane 分散でその候補を拾った枠。複数 signal 銘柄では `selection_lane` と異なることがある）
   - `signals` の重なり（複数 signal は tie-break として優先するが、それだけで primary thesis にしない）
   - signal lane の種類（cash / CF / sales / valuation のどの割安タイプか）
   - primary metric の乖離幅（業種中央値比・過去自己比較・OCF yield・cash 比率）
   - 同業種の中で相対的に過剰売られ
   - 最大で 1 回の selection あたり **3-5 銘柄** に絞る（1 人運用で深掘りできる現実的な上限）

### 2.1 outlook 未更新時の対応

- 最新 outlook が古く、その後に重大 brief が出て gate 判定に影響する場合:
  - 該当 brief を research の `brief_refs` に追加
  - gate を **保守側にのみ** 手動上書き可（tailwind → neutral、neutral → headwind。逆方向の上書き不可）
  - 詳細: [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md)

### 2.2 銘柄IR確認の必須化

research 対象に選んだ銘柄は、業種を問わず **会社IRを一次情報として必ず確認する**。screening や外部分析は候補選定の補助であり、採用 / 見送り / 保留の判断を確定する根拠にはしない。

- 最低限、直近の決算短信、決算説明資料、会社説明会 Q&A、有価証券報告書 / 統合報告書、中期経営計画、株主還元・自己株式取得・配当関連の適時開示を確認する
- cash-rich signal では有利子負債・偶発債務を一次情報で確認する
- CF signal では営業 CF の一過性要因、運転資本、季節性を確認する
- candidates / select の `freshness_warnings` に `source_family: edinet-metrics` がある場合、strict net cash / cash-rich / FCF 系の primary または supporting signal では、EDINET 由来の cash / debt / net cash / EV / equity / share count / capex / fcf_ttm を最新の会社IR・適時開示・有報で再確認する。warning が残ったまま未確認なら `decision: accepted` にしない
- 会社IRで確認できた事実、会社IRでは確認できず外部 estimate に留めた情報、外部AI / 二次分析から修正した数値を research 本文の source verification log に分けて残す
- 会社IRが未確認の銘柄は `decision: accepted` にしない。情報不足なら `pending` または `skipped` とし、未確認項目を明記する

### 2.3 複数 signal hit の扱い

research file の `playbook` は primary thesis を 1 つだけ選ぶ。複数 signal が hit した場合は、`select` の `selection_lane`、macro gate、最も検証したい割安仮説を見て primary を決め、残りは `supporting_signals` と本文「Candidate signals + valuation snapshot」に列挙する。`recommendation_lane` は lane 分散の選定理由であり、primary thesis ではない。

複数 signal hit は採用理由ではなく、検証優先度を上げる材料である。例えば cash-rich と CF が両方 hit しても、有利子負債・運転資本・一過性 CF を一次情報で確認できなければ accepted にしない。

### 2.4 Portfolio macro risk budget

research が実注文や実資金 allocation に直結する場合、`Macro gate` section には sector の
`tailwind / neutral / headwind` だけでなく、portfolio 全体の risk budget を短く明記する。

最低限、以下を確認する:

- 最新 outlook の base / downside / upside scenario と、現在どの scenario に近いか
- 今後 1-2 週間の macro trigger（米 CPI / 雇用統計 / FOMC / BOJ / 地政学 / 原油など）
- その trigger 前に実資金をどこまで投入するか、通過後にどこまで増やすか
- 投資可能な実資金全体と、一時的な様子見枠・tactical cap を分けているか
- 同一 sector / 同一 thesis への集中度が、個別銘柄の signal 強度に比べて過大でないか
- 決算直前の候補を先行買いする場合、取り逃しリスクと event risk のどちらが大きいか

macro gate は「採用可否の boolean」ではなく、position size と timing を決める上位制約として扱う。
個別銘柄が tailwind でも、downside 確率が高く、主要 macro trigger の直前であれば、初期投入を
抑え、trigger 通過後に追加する。

## 3. Path と命名

```
records/04-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
```

- `<ticker>`: 4 文字の英数字文字列
- `<playbook>`: `valuation-reversion` / `strict-net-cash-discount` / `fcf-yield-discount` / `cash-rich-asset-discount` / `cashflow-yield-discount` / `sales-discount-growth`

## 4. Front matter 必須項目

```yaml
---
ticker: "7203"
name: "トヨタ自動車"
playbook: valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth
supporting_signals:
  - cashflow-yield-discount
decision: accepted | skipped | pending
candidates_ref: records/03-candidates/YYYY/MM/YYYY-MM-DD.yaml
outlook_ref: records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml
brief_refs:
  - records/01-brief/YYYY/MM/YYYY-MM-DD-*.yaml
ai-draft: true | false
published_at: "ISO 8601"
tradable_at: "ISO 8601"
macro_gate: tailwind | neutral | headwind
macro_gate_override: "..."
overrides:
  - type: decision_flip | candidate_absence | universe_drop | real_concentration_cap
    prior_state_ref: "path or commit:path"
    prior_state: "..."
    new_state: "..."
    reason: "..."
external_refs:
  - records/_external/<source>/YYYY-MM-DD-<topic>.md
position_size_oku: 0.01
hypothetical_position_size_oku: 0.005
avg_turnover_oku: 5.0
adv_participation_pct: 0.2
market_cap_oku: 936
sector_33: "情報・通信業"
valuation:
  per_forward: 数値 | null
  per_trailing: 数値 | null
  pbr: 数値 | null
  ev_ebitda: 数値 | null
  p_s: 数値 | null
  pcfr: 数値 | null
  ocf_yield: 数値 | null
  fcf_yield: 数値 | null
  net_cash_to_market_cap: 数値 | null
  cash_to_market_cap: 数値 | null
  price_to_equity: 数値 | null
  equity_ratio: 数値 | null
  primary_metric: ["pbr", "ocf_yield"]
---
```

- `playbook` は primary thesis を 1 つだけ持つ。複数 signal がある場合は `supporting_signals` と本文 §3 に列挙する
- `outlook_ref` は **必須**
- `macro_gate` が `headwind` の場合は採用不可（原則）。採用する場合は `macro_gate_override` が必須
- `position_size_oku` は仮定資本 1 億円ベース。採用 position 1.0% は `0.01` 億円として記録する
- `adv_participation_pct` は `5.0` 以上で hard reject
- 配当利回りは front matter に含めず、本文の株主還元確認で扱う

## 5. Packet 必須項目（本文、14 項目）

本文は [`../templates/research.md`](../templates/research.md) の 14 項目を使う:

1. Thesis（一文、why now / why this stock、primary signal を明示）
2. Macro gate（tailwind / neutral / headwind）+ 判定根拠
3. Candidate signals + valuation snapshot
4. 割安の原因仮説
5. 反対仮説 - 構造的理由
6. Catalyst
7. Price reaction
8. Crowding
9. 株主還元確認（配当政策 / 自社株買い / DOE or 配当性向 / 減配リスク）
10. ミクロ 4 軸寄与度表
11. Entry 条件
12. Exit 条件
13. Invalidation + Pre-mortem
14. Position size + 採用判定

### 5.1 schema 検証

front matter の必須 field と `playbook` ごとの本文 section 構造は `baibai-loop-validate` で検査される。playbook 別の本文 section schema は [`/records/_playbooks/`](/records/_playbooks/) 配下に `<name>.schema.yaml` として分離してあり、新 playbook を追加した時は同名 schema YAML を置くだけで validate に反映される。手元では `uv run baibai-loop-validate --target research` で個別に走らせられる。

research decision は `baibai-loop-ledger sync` で [`records/_ledger/`](./ledger.md) の JSONL に正規化される。`accepted` と `pending` は paper ledger、`skipped` は skipped ledger に残す。

## 6. 採用判定

- `macro_gate = headwind` は原則採用不可（outlook で対象業種/地域が `null` の場合は neutral 扱いで判定可）
- primary signal の割安判定が成立している
- 反対仮説を考えて「構造的 trap ではない」と説明できる
- strict net-cash signal では EDINET 由来 debt / cash の tag source と有利子負債の範囲を一次情報で確認している
- cash-rich signal では有利子負債確認を完了している
- FCF signal では capex source、設備投資の一過性、維持投資 / 成長投資の区別を確認している
- candidates / select の `freshness_warnings` が EDINET metrics の古さを示す場合、該当 warning の event title と日付を確認し、最新の balance sheet / cash flow / share count への影響を一次情報で確認している
- OCF signal では営業 CF の期間正規化、一過性要因、悪化有無を確認している
- sales signal では売上成長が残り、営業赤字の場合は CFO プラスまたは赤字縮小が確認できる
- catalyst は必須ではないが、存在する場合は freshness と一次ソースを記録する
- crowding が踏み上げリスクと逆回転リスクの両方で許容範囲
- kill switch に抵触しない（決算またぎ / 日銀会合前日 / FOMC 前日）
- position size が signal 数と流動性条件に照らして過大でない

### 6.1 Position sizing

- single signal: 最大 1%
- 複数 signal: 最大 2%
- `adv_participation_pct >= 5.0` は hard reject

現在の実資金や tactical cap が小さい場合、paper proxy の ADV cap は実運用ではほぼ拘束しない。paper proxy は検証用の統一尺度として残し、実資金の集中度は trade 側の `real_*` / `tactical_*` fields で別管理する。

## 7. trades への接続

採用した research の front matter path は、`records/05-trades/` の `research_ref` で参照される:

```yaml
research_ref: records/04-research/2026/05/2026-05-10-7203-valuation-reversion.md
```

## 8. AI の役割境界（packet 項目単位）

| 項目 | 内容 | AI 可 | 人間のみ |
| --- | --- | --- | --- |
| 1 | Thesis ドラフト | ○ | 最終確認 |
| 2 | Macro gate 判定ドラフト | ○ | **確定は人間** |
| 3 | Candidate signals / valuation snapshot | ○ | 数値正誤確認 |
| 4 | 割安の原因仮説ドラフト | ○ | 確定 |
| 5 | 反対仮説ドラフト | ○ | 確定 |
| 6 | Catalyst 種別・経過営業日ドラフト | ○ | **一次ソース URL 確認は人間** |
| 7 | Price reaction 機械集計 | ○ | |
| 8 | Crowding 指標取得 | ○ | |
| 9 | 株主還元確認ドラフト | ○ | 一次ソース確認 |
| 10 | 4 軸寄与度初期評価 | ○ | 確定 |
| 11 | Entry 条件ドラフト | ○ | 確定 |
| 12 | Exit 条件ドラフト | ○ | 確定 |
| 13 | Invalidation / Pre-mortem ドラフト | ○ | 採用条件確定 |
| 14 | Position size 判定ドラフト | ○ | **最終採用判定は人間** |

AI 下書きは front matter `ai-draft: true` で識別、人間確認後 `false` に更新。

## 8.1 commit 前 self-review (anti-pattern との対応)

- [ ] **AP-01** (一次情報直接確認): 事業構造や catalyst を断定する場合、会社IR / 有報 / 決算説明資料 / 適時開示のいずれかに直接 URL を紐付けたか
- [ ] **会社IR必須確認**: 直近決算短信 / 決算説明資料 / Q&A / 有報または統合報告書 / 中計 / 株主還元関連開示を確認したか
- [ ] **AP-02** (数値検算): `adv_participation_pct = position_size_oku / avg_turnover_oku * 100` を検算したか
- [ ] **AP-03** (株価異常値の corporate action 確認): candidates の `price_change_60d` / `price_change_4w` が大きい銘柄は corporate action の有無を確認したか
- [ ] **AP-04** (schema / 実装の意味): candidates の `signals` / `metrics` / `sector_relative_strength_percentile` の意味を `src/baibai_loop/screening/metrics.py` と `rules.py` で確認したか
- [ ] **EDINET freshness warning**: candidates / select の `freshness_warnings` がある場合、該当 event 後の cash / debt / net cash / EV / equity / share count / capex / FCF 影響を一次情報で再確認したか
- [ ] **AP-06** (ref 整合性): `outlook_ref` / `candidates_ref` / `brief_refs` の 3 ref が valid パスか
- [ ] **AP-07** (kill switch と日付): `tradable_at` 周辺に決算・日銀会合・FOMC が無いか
- [ ] **AP-08** (validator 抜け道): skipped では `position_size_oku: 0` + `adv_participation_pct: 0` を守ったか
- [ ] **外部 AI / 二次分析の検証**: 他AI・証券サイト・ニュース要約の投資判断をそのまま転記していないか
- [ ] **system signal override の明示**: 最新 candidates からの不在、universe drop、macro headwind、実資金集中度超過などを上書きして採用する場合、`overrides` と本文に理由を残したか

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位、事実と分析の分離）
- [`../architecture.md`](../architecture.md): 全体構造
- [`candidates.md`](./candidates.md): source となる candidates の仕様
- [`outlook.md`](./outlook.md): Macro gate source の仕様
- [`../screening/principles.md`](../screening/principles.md): screening / playbook 原則
- [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md): Macro gate 判定手順
- [`../templates/research.md`](../templates/research.md): template
- [`/records/_playbooks/`](/records/_playbooks/): playbook 本体
