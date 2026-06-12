# components/reviews.md

Baibai-Loop の **review / attribution** 成分の運用仕様。事後検証、missed opportunity tracking、playbook attribution で feedback loop を閉じる。全体構造は [`../architecture/system-overview.md`](../architecture/system-overview.md)、概念モデルは [`../concepts.md`](../concepts.md) を参照。

## 1. 役割

- `records/06-trades/` の完結（exit 済み）に対し、**決済後 +15 / +30 営業日レビュー** を作成
- **月次 retro** で成功/失敗分類、missed opportunity、playbook 改訂判断を扱う
- Outcome を evidence hit、macro context fit、sizing、execution、playbook へ帰属し、次回の screening / investment memo に反映する

## 2. 種類

### 2.1 個別 review

- trade 1 件ごとに作成
- 決済後 +15 営業日、+30 営業日の 2 時点で追記
- 決済前 exit（kill switch 適用 etc.）も必須記録

### 2.2 月次 retro

- 毎月 1 回（月末または翌月初）
- 当月の全個別 review + missed opportunity tracking を集約
- 失敗分類の再集計、playbook 改訂判断、次周回の変更点

## 3. Path と命名

```
records/07-reviews/YYYY/MM/YYYY-MM-DD-<ticker>.md        # 個別 review（exit 日付）
records/07-reviews/YYYY/retro-YYYYMM.md                  # 月次 retro
```

## 4. 個別 review の Front matter

```yaml
---
ticker: "7203"
decision_event_id: decision-YYYYMMDD-<ticker>-review-target
research_ref: records/05-research/YYYY/MM/YYYY-MM-DD-<ticker>-<playbook>.md
trade_ref: records/06-trades/YYYY/MM/YYYY-MM-DD-<ticker>.md
playbook_id: valuation-reversion | strict-net-cash-discount | fcf-yield-discount | cash-rich-asset-discount | cashflow-yield-discount | sales-discount-growth
classification: success | failure | invalidated | inconclusive
verified_at: "YYYY-MM-DDTHH:MM:SS+09:00"
outcome:
  horizon_bd: 15 | 30
  start_price_basis: first_fill_vwap_yen | research_max_entry_price_yen | candidate_run_close_adjusted_close
  start_price_yen: 数値
  end_price_yen: 数値
  gross_return_pct: 数値
  market_baseline_return_pct: 数値
  sector_baseline_return_pct: 数値
  market_relative_return_pct: 数値
  sector_relative_return_pct: 数値
  primary_relative_baseline: market | sector
  primary_relative_return_pct: 数値
  execution_costs_yen: 数値
  net_return_pct: 数値
attribution_targets:
  - type: evidence_hit | macro_context | sizing | execution | playbook
    target_id: "id"
    effect: helped | hurt | neutral | unknown
    confidence: high | medium | low
structured_field_provenance:
  outcome: machine_calculated
  attribution_targets: analyst_written | llm_drafted_analyst_confirmed
---
```

- `decision_event_id` は decision register の判断イベントへ join する anchor。
- `outcome` は pinned market data / baseline snapshot から再計算できる値にする。
- `attribution_targets` は outcome を evidence、macro context、sizing、execution、playbook のどこへ帰属させるかを構造化する。

### 4.1 Price evidence

Review / retro の価格は J-Quants(`data/screening/market.sqlite` の bars)を source とし、tracking は `baibai-loop-ledger sync` が機械的に埋める。bars が未到達で埋まらない場合は、bars が貯まってから sync を再実行する(forward-only に埋まる)。

J-Quants が長期に使えない場合だけ、同一 basis の public daily quote を手動で参照し、review / retro 本文の `Price evidence` に source URL、取得日時、評価日、price basis、benchmark と同一 basis かを書く。daily close と intraday last は混ぜず、basis が揃わない・corporate action 未確認の評価は provisional とし、classification は `inconclusive` 寄りに扱う。

## 5. 月次 retro の Front matter

```yaml
---
retro_month: "YYYY-MM"
approved_decisions: 整数
submitted_orders: 整数
filled_positions: 整数
closed_positions: 整数
missed_opportunities: 整数
failure_class_counts:
  材料誤読: 整数
  既に織り込み済み: 整数
  マクロ逆風: 整数
  ポジショニング / 流動性: 整数                          # 概念上は positioning / liquidity risk
  流動性不足: 整数
  ルール違反: 整数                    # 別枠、playbook 改訂の input にしない
success_class_counts:
  仮説的中: 整数
  catalyst 反応: 整数
  macro tailwind: 整数
  timing 一致: 整数
playbook_revision_decision: "据え置き" | "小改訂" | "大幅改訂"
next_cycle_changes:
  - "変更点 1"
  - "変更点 2"
price_missing_counts:
  plus_15bd: 整数
  plus_30bd: 整数
---
```

## 6. 失敗分類（固定 6 種）

個別 review で `failure_class` に記入する分類:

| 分類 | 定義 |
| --- | --- |
| 材料誤読 | 一次材料の解釈が誤っていた（例: 上振れが一事業限定だった） |
| 既に織り込み済み | 採用時点で市場がすでに織り込んでいた |
| マクロ逆風 | Macro context fit の誤り、または前提が保有中に headwind へ反転 |
| ポジショニング / 流動性（positioning / liquidity） | 空売り残高・日々公表信用・特別注意・出来高不足など positioning / liquidity risk |
| 流動性不足 | 想定より出来高が伴わず entry/exit が困難 |
| ルール違反 | playbook / kill switch / position sizing 等のルール違反 |

**ルール違反トレードの扱い**: 赤ラベルで識別、playbook 改訂の input には使わない（ルール違反は playbook の不備ではなく運用の不備）。

## 7. 成功分類（初期 4 種、拡張可）

個別 review で `success_class` に記入する分類:

| 分類 | 定義 |
| --- | --- |
| 仮説的中 | research の thesis が想定通りに実現 |
| catalyst 反応 | 採用時の catalyst に市場が遅れて反応 |
| macro tailwind | マクロ追い風が想定以上に効いた |
| timing 一致 | entry のタイミング判断が正解 |

将来の retro で成功/失敗分類の再分類を行う（四半期ごと）。

## 8. Missed opportunity tracking

- `records/05-research/` で見送り / 保留になった候補、採用したが order submit しなかった候補を、月次 retro で missed opportunity として追跡する
- **追跡タイミング**: candidate 作成日 +15 / +30 営業日時点を基本に、market / sector baseline に対する relative return も見る
- **追跡方法**: 月次 retro のタイミングでまとめて実施。日次作業に乗せない
- Macro context fit で見送った候補も追跡し、headwind / not_matched 判定が過度に保守的でないか検証する
- 結果は retro 本文に集計し、playbook / screening threshold / macro context fit のどこを直すべきかに接続する

## 9. Retro での feedback ループ

### 9.1 判断基準

- サンプル数が playbook 別で 10 件未満 → **playbook 据え置きを許容**
- 代わりに checklist 差分（packet の追加チェック欄）を提案する
- 自由記述 + 反対仮説自由記述の頻出キーワードを 3-5 個抽出し、次周回で意識するポイントに落とす
- **Macro context fit の判定精度** を集計（tailwind 判定した銘柄の +15/+30 パフォーマンス、headwind / not_matched 判定した不採用銘柄のパフォーマンス）
- **+15/+30 価格欠損件数** を必ず出し、tracking 未完了のまま評価しない
- **Valuation trap が 0 件だった場合**: 「サンプル不足のため valuation trap 耐性は未検証」と明記し、次周回の重点観察項目にする

### 9.2 受け渡し条件

- 次周回の運用変更点が **3 行以内で要約できる** こと
- 変更点は `playbook` / `screening 閾値` / `macro context` 運用 / `research` 選定基準 のどこに反映するかも明示

## 10. AI の役割境界

| 作業 | AI 可 | 人間のみ |
| --- | --- | --- |
| review 下書き生成 | ○ | |
| net_return_pct / relative return 計算 | ○ | |
| missed opportunity の騰落集計 | ○ | |
| **失敗分類の確定** | | ○ |
| **成功分類の確定** | | ○ |
| **playbook 改訂判断** | | ○ |

## 11. 参考

- [`../philosophy.md`](../philosophy.md): 思想（feedback loop 先行）
- [`../architecture/system-overview.md`](../architecture/system-overview.md): 全体構造
- [`../concepts.md`](../concepts.md): 投資判断ドメインモデル
- [`trades.md`](./trades.md): source となる trades の仕様
- [`research.md`](./research.md): investment memo source
- [`../screening/failure-taxonomy.md`](../screening/failure-taxonomy.md): 失敗分類詳細
- [`../templates/review.md`](../templates/review.md): 個別 review template
- [`../templates/retro-monthly.md`](../templates/retro-monthly.md): 月次 retro template
