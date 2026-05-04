# components/research.md

Baibai-Loop 4 成分アーキテクチャの **(d) 個別銘柄リサーチ** の運用仕様。candidates × outlook から選定した個別銘柄の深掘り packet 本体。全体構造は [`../architecture.md`](../architecture.md)、スクリーニング詳細は [`../screening/`](../screening/) を参照。

## 1. 役割

- `records/03-candidates/` × `records/02-outlook/` から選定した個別銘柄について、**一時的割安の原因仮説・反対仮説・catalyst・crowding を深く分析**
- 採用判定 / 見送り / 保留を記録（packet）
- Micro track の分析層、4 成分統合の出力点

## 2. 選定プロセス（candidates × outlook → 候補絞り込み）

`candidates` (ミクロ事実) × `outlook` (マクロ見解) の 2 軸統合を具体化する。

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

### 2.3 outlook が存在しない場合

- `records/02-outlook/` に最新の outlook がない場合は、research 作成前に outlook を作成する
- 初回作成手順: [`outlook.md`](./outlook.md) §3 を参照

### 2.4 銘柄IR確認の必須化

research 対象に選んだ銘柄は、業種を問わず **会社IRを一次情報として必ず確認する**。screening や
外部分析は候補選定の補助であり、採用 / 見送り / 保留の判断を確定する根拠にはしない。

- 最低限、直近の決算短信、決算説明資料、会社説明会 Q&A、有価証券報告書 / 統合報告書、
  中期経営計画、株主還元・自己株式取得・配当関連の適時開示を確認する
- 会社IRで確認できた事実、会社IRでは確認できず外部 estimate に留めた情報、外部AI / 二次分析から
  修正した数値を research 本文の source verification log に分けて残す
- 会社IRが未確認の銘柄は `decision: accepted` にしない。情報不足なら `pending` または `skipped` とし、
  未確認項目を明記する

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
outlook_ref: records/02-outlook/YYYY/MM/outlook-YYYY-MM-DD-*.yaml       # 必須
brief_refs:                                        # 任意、outlook 後に出た緊急 brief 時のみ
  - records/01-brief/YYYY/MM/YYYY-MM-DD-*.yaml
ai-draft: true | false                             # AI 下書きフラグ
published_at: "ISO 8601"
tradable_at: "ISO 8601"
macro_gate: tailwind | neutral | headwind          # outlook 判定結果
macro_gate_override: "..."                         # headwind 採用時のみ必須
overrides:                                         # 任意。system signal を上書きする場合は必須運用
  - type: decision_flip | candidate_absence | universe_drop | real_concentration_cap
    prior_state_ref: "path or commit:path"
    prior_state: "..."
    new_state: "..."
    reason: "..."
external_refs:                                     # 任意。外部AI / 二次分析を参照する場合
  - records/_external/<source>/YYYY-MM-DD-<topic>.md
position_size_oku: 0.01                            # 建玉 proxy (億円)。skipped は 0、accepted/pending は > 0
hypothetical_position_size_oku: 0.005              # 任意。skipped で参考値として記録する場合
avg_turnover_oku: 5.0                              # candidates 由来の 20 日平均売買代金 (億円)。adv_participation_pct を書く場合は > 0 必須 (validator 強制)
adv_participation_pct: 0.2                         # position_size_oku / avg_turnover_oku * 100。validator が ±5% で整合チェック
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

- `outlook_ref` は **必須**
- `brief_refs` は任意。outlook 後に gate 判定に影響する緊急 brief を参照した場合のみ追加
- `macro_gate` が `headwind` の場合は採用不可（原則）
- `decision: accepted` かつ `macro_gate: headwind` の場合は `macro_gate_override` が必須
- `tradable_at` は注文または約定が可能になる最初の市場時刻。休場日・立会時間外に注文を入れた場合、
  `published_at` / `order_date` より後の次回立会時刻になる
- `overrides` は、直前の `skipped` 判定、最新 candidates からの不在、universe drop、実資金集中度超過など、
  system signal を人間判断で上書きする場合に残す
- `external_refs` は外部 AI / 二次分析を参照する場合に使う。生原稿は [`/records/_external/`](/records/_external/)
  に保存し、本文 §14 source verification log で `external_refs[]` ごとに 採用 / 修正 / 未採用 を表で残す
- `position_size_oku` は仮定資本 1 億円ベース。採用 position 1.0% は `0.01` 億円として記録する
- `adv_participation_pct` は `5.0` 以上で hard reject
- `market_cap_oku` / `sector_33` は candidates から転記し、tier rule と sector 集中 warning の検証に使う
- 配当利回りはスコープ外のため front matter に含めない

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

- `macro_gate = headwind` は原則採用不可（outlook で対象業種/地域が `null` の場合は neutral 扱いで判定可）
- valuation 軸で割安判定（業種中央値・過去自己比較）が成立
- 反対仮説を考えて「構造的 trap ではない」と確信できる
- catalyst（P-B のみ必須）が freshness ≦ 60 営業日
- crowding が踏み上げリスクと逆回転リスクの両方で許容範囲
- kill switch に抵触しない（決算またぎ / 日銀会合前日 / FOMC 前日）
- position size が時価総額別上限を満たす
- 200-500 億帯は **P-B のみ採用可**（P-A 単独は不可）。例外運用が必要なら `macro_gate_override` で明示理由を残す

### 6.3 取引コスト・スリッページの想定

利確 target は playbook 仕様に従う (P-A `valuation-mean-reversion-v1` の場合は entry 時の
業種中央値比 valuation gap が 0% に回帰した時点を指す。詳細は
[`/records/_playbooks/valuation-mean-reversion-v1.md`](../../records/_playbooks/valuation-mean-reversion-v1.md))。
target % は entry 時の cheap 度合いで決まり、固定 minimum を持たない。

参考までに観測上の典型値レンジ (binding ではない):

- 既存 P-A 採用 packets の利確 target は概ね +10% 〜 +30% に収まる (例: 5410 合同製鐵
  は +11%、4716 日本オラクル は +20% 程度)
- 業種中央値比 -20% から復帰する想定なら +25% 前後 が typical、-10% からの復帰なら +11%
  前後

採用判定時の利確 / 損切ターゲットは **net of cost** で評価する。本システムは backtest を行
わないため厳密なシミュレーションコストは引かないが、入退出計画では以下の概算を頭に
置く:

- **手数料 (round-trip)**: ネット証券・現物取引で約定代金 0.05-0.10% 想定 (1 億円
  portfolio で 100-200万円 / position の場合の典型レンジ)。新興系ブローカーで定額制を
  使う場合はさらに低くなる
- **スプレッド・スリッページ**: liquid な大型 cap (avg_turnover_oku ≧ 10) では 0.05% 程度、
  中小型 (3-10 億 / 日) では 0.10-0.20% を見込む。`adv_participation_pct` 上限 1% は
  この観点でも overflow 防止として効く
- **対 target return の影響**: round-trip cost 合計 0.3-0.5% は target +10-30% の playbook
  に対し target の 1-5% に相当。target を crossing する判定には実用上のマージンが確保
  できる範囲だが、target +5% 未満を狙う short-horizon の playbook を新設する場合は
  cost ratio がきつくなる点に注意

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

## 8.1 commit 前 self-review (anti-pattern との対応)

research packet を書いた / 更新した後、commit 前に以下を必ず確認する。詳細チェックリストは
[`../anti-patterns.md`](../anti-patterns.md) を参照:

- [ ] **AP-01** (一次情報直接確認): TSMC / NVIDIA / 顧客企業等の事業構造を断定する場合、有価証券報告書 / 決算説明資料 / 統合報告書 / IR press release のいずれかに直接 URL を紐付けたか。アナリスト試算や業界レポート由来は明示的に「外部 estimate」と区別したか。**source の policy / rate / date / scenario が本文主張と一致しているか** (URL を貼っただけで終わらせない)
- [ ] **会社IR必須確認**: research 対象銘柄について、業種を問わず直近決算短信 / 決算説明資料 /
      Q&A / 有価証券報告書または統合報告書 / 中計 / 株主還元関連開示を確認したか。未確認のまま
      `decision: accepted` にしていないか
- [ ] **AP-02** (数値検算): `adv_participation_pct = position_size_oku / avg_turnover_oku * 100` を電卓 / Python で検算したか。利確 target の % は EPS 一定で `(target_per / current_per - 1) * 100` で計算したか
- [ ] **AP-03** (株価異常値の corporate action 確認): candidates の `price_change_60d` / `price_change_4w` が ±50% を超える、または `self_range_percentile` が下位 5% 以下の銘柄は、研究進める前に EDINET / TDnet / 適時開示で 60 日 / 4 週期間内の株式分割 / 併合 / 合併 / TOB の有無を必ず確認したか
- [ ] **AP-04** (schema / 実装の意味): candidates の `sector_relative_strength_percentile` は **sector level の rank** であって個別銘柄の同業種内相対強度ではない。同様に `threshold_hit` / `metrics_breakdown` も `src/baibai_loop/screening/metrics.py` と `rules.py` で意味を確認したか
- [ ] **AP-05** (顧客 / 競合 / 親会社の断定回避): 公式製品ページや業界記事だけを根拠に「主要顧客 = X / Y / Z」と固有名詞を断定していないか。有報・決算説明資料で確認できない顧客名は本 packet 内では断定せず「主要半導体メーカー (有報確認後に列挙)」のような plain holder で書く
- [ ] **AP-06** (ref 整合性): `outlook_ref` / `candidates_ref` / `brief_refs` の 3 ref が valid パスか、対応 file が実在するか。引用する fact は brief 経由で参照しているか
- [ ] **AP-07** (kill switch と日付): `tradable_at` 周辺に決算 (会社四季報 / TDnet で確認)・日銀会合・FOMC が無いか、`next_earnings_date` が candidates から正しく取れているか
- [ ] **AP-08** (validator 抜け道): `adv_participation_pct` を front matter に書く場合は `avg_turnover_oku` も併記 (validator が required 化)。`decision: skipped` では `position_size_oku: 0` + `adv_participation_pct: 0` 強制 (validator)、参考値は `hypothetical_position_size_oku` に分離。`avg_turnover_oku` は `candidates_ref` 対応 ticker と ±5% で整合 (validator が warning レベルで check)
- [ ] **外部 AI / 二次分析の検証**: 他AI・証券サイト・ニュース要約の投資判断を取り込む場合、結論をそのまま転記せず、少なくとも会社IR / 決算短信 / 決算説明資料 / Q&A / 取引所休日 / candidates のいずれかで主要数値を再確認したか。確認できた事実、修正した数値、未採用の二次情報を research の source verification log に分けて残したか
- [ ] **system signal override の明示**: 直前の `decision: skipped`、最新 candidates からの不在、
      universe drop、macro headwind、実資金集中度超過などを上書きして採用する場合、`overrides` と本文に
      prior state / override reason / evidence を残したか

## 9. 参考

- [`../philosophy.md`](../philosophy.md): 思想（マクロ優位 76/24、事実と分析の分離）
- [`../architecture.md`](../architecture.md): 全体構造
- [`candidates.md`](./candidates.md): source となる candidates の仕様
- [`outlook.md`](./outlook.md): Macro gate source の仕様
- [`../screening/principles.md`](../screening/principles.md): Playbook P-A / P-B 定義
- [`../screening/macro-gate-procedure.md`](../screening/macro-gate-procedure.md): Macro gate 判定手順
- [`../templates/research.md`](../templates/research.md): template（packet 13 項目）
- [`/records/_playbooks/valuation-mean-reversion-v1.md`](/records/_playbooks/valuation-mean-reversion-v1.md): P-A 本体
- [`/records/_playbooks/valuation-catalyst-confirmation-v1.md`](/records/_playbooks/valuation-catalyst-confirmation-v1.md): P-B 本体
